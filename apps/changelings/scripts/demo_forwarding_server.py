#!/usr/bin/env python3
"""Demo script that starts test backend servers and the forwarding server.

This demonstrates the full flow with two real backends:
1. file-browser: serves files from a directory using a simple web UI with navigation
2. ws-echo: a WebSocket echo server with a chat-like UI

Usage:
    uv run python apps/changelings/scripts/demo_forwarding_server.py
"""

import os
import secrets
import sys
import tempfile
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi import Request
from fastapi import WebSocket
from fastapi import WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.responses import PlainTextResponse
from fastapi.responses import Response

from imbue.changelings.forwarding_server.app import create_forwarding_server
from imbue.changelings.forwarding_server.auth import FileAuthStore
from imbue.changelings.forwarding_server.backend_resolver import StaticBackendResolver
from imbue.changelings.primitives import OneTimeCode
from imbue.mng.primitives import AgentId


def _escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _render_directory_listing(dir_path: str, url_path: str) -> HTMLResponse:
    """Render an HTML directory listing."""
    entries: list[str] = []
    try:
        items = sorted(os.listdir(dir_path))
    except OSError:
        items = []

    # Use absolute links -- the forwarding server rewrites them transparently
    if url_path != "/":
        parent = "/".join(url_path.rstrip("/").split("/")[:-1])
        parent_href = parent if parent else "/"
        entries.append(f'<li><a href="{parent_href}">../</a></li>')

    for item in items:
        if item.startswith("."):
            continue
        full = os.path.join(dir_path, item)
        display = f"{item}/" if os.path.isdir(full) else item
        href = f"{url_path.rstrip('/')}/{item}"
        entries.append(f'<li><a href="{href}">{_escape_html(display)}</a></li>')

    entries_html = "\n    ".join(entries) if entries else "<li>(empty)</li>"

    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>Files: {url_path}</title></head>
<body style="font-family: system-ui; padding: 20px;">
  <h1>File Browser</h1>
  <h2>{_escape_html(url_path)}</h2>
  <ul style="font-size: 16px; line-height: 1.8;">
    {entries_html}
  </ul>
  <hr>
  <p style="color: gray; font-size: 12px;">
    Served via changelings forwarding server. Path shown by browser:
    <code id="path"></code>
  </p>
  <script>document.getElementById('path').textContent = window.location.pathname;</script>
</body>
</html>""")


def _file_browser_root(request: Request) -> HTMLResponse:
    """Serve the root directory listing for the file browser."""
    browse_dir: str = request.app.state.browse_dir
    return _render_directory_listing(browse_dir, "/")


def _file_browser_browse(path: str, request: Request) -> Response:
    """Serve a file or directory listing for the file browser."""
    browse_dir: str = request.app.state.browse_dir
    full_path = os.path.join(browse_dir, path)
    if not os.path.realpath(full_path).startswith(os.path.realpath(browse_dir)):
        return PlainTextResponse("Access denied", status_code=403)
    if os.path.isdir(full_path):
        # Redirect to trailing slash so relative links resolve correctly
        if not str(request.url).endswith("/"):
            return Response(status_code=307, headers={"Location": f"{path}/"})
        return _render_directory_listing(full_path, f"/{path}")
    elif os.path.isfile(full_path):
        try:
            content = open(full_path).read()
        except (OSError, UnicodeDecodeError):
            return PlainTextResponse("Cannot read file", status_code=500)
        # Absolute back link -- the forwarding server rewrites it
        parent_path = "/" + "/".join(path.split("/")[:-1])
        back_href = parent_path if parent_path != "/" else "/"
        return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head><title>{path}</title></head>
<body style="font-family: monospace; padding: 20px;">
  <p><a href="{back_href}">Back</a></p>
  <h2>{path}</h2>
  <pre style="background: rgb(245, 245, 245); padding: 16px; overflow: auto;">{_escape_html(content)}</pre>
</body>
</html>""")
    else:
        return PlainTextResponse("Not found", status_code=404)


def _create_file_browser_backend(browse_dir: str) -> FastAPI:
    """Create a backend that serves a file browser for a directory."""
    app = FastAPI()
    app.state.browse_dir = browse_dir
    app.add_api_route("/", _file_browser_root)
    app.add_api_route("/{path:path}", _file_browser_browse)
    return app


_WS_ECHO_HTML = """<!DOCTYPE html>
<html>
<head><title>WebSocket Echo</title></head>
<body style="font-family: system-ui; padding: 20px; max-width: 600px; margin: 0 auto;">
  <h1>WebSocket Echo</h1>
  <p>Type a message and press Send. The server will echo it back.</p>
  <div id="messages" style="
    border: 1px solid rgb(204, 204, 204); padding: 12px; height: 300px;
    overflow-y: auto; margin-bottom: 12px; background: rgb(250, 250, 250);
    font-family: monospace; font-size: 14px;
  "></div>
  <form id="form" style="display: flex; gap: 8px;">
    <input id="input" type="text" placeholder="Type a message..."
      style="flex: 1; padding: 8px; font-size: 14px; border: 1px solid rgb(204, 204, 204); border-radius: 4px;">
    <button type="submit" style="padding: 8px 16px; font-size: 14px; cursor: pointer;">Send</button>
  </form>
  <p id="status" style="color: gray; font-size: 12px; margin-top: 8px;">Connecting...</p>
  <script>
    const messages = document.getElementById('messages');
    const form = document.getElementById('form');
    const input = document.getElementById('input');
    const status = document.getElementById('status');

    function addMessage(text, fromServer) {
      const div = document.createElement('div');
      div.style.marginBottom = '4px';
      div.style.color = fromServer ? 'green' : 'blue';
      div.textContent = (fromServer ? 'Server: ' : 'You: ') + text;
      messages.appendChild(div);
      messages.scrollTop = messages.scrollHeight;
    }

    const wsProtocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const ws = new WebSocket(wsProtocol + '//' + location.host + '/ws');

    ws.onopen = () => { status.textContent = 'Connected'; };
    ws.onclose = () => { status.textContent = 'Disconnected'; };
    ws.onerror = () => { status.textContent = 'Error'; };
    ws.onmessage = (e) => { addMessage(e.data, true); };

    form.onsubmit = (e) => {
      e.preventDefault();
      if (input.value && ws.readyState === WebSocket.OPEN) {
        addMessage(input.value, false);
        ws.send(input.value);
        input.value = '';
      }
    };
  </script>
</body>
</html>"""


def _ws_echo_root() -> HTMLResponse:
    """Serve the WebSocket echo chat UI."""
    return HTMLResponse(_WS_ECHO_HTML)


async def _ws_echo_handler(websocket: WebSocket) -> None:
    """Handle WebSocket echo connections."""
    await websocket.accept()
    connected = True
    while connected:
        try:
            data = await websocket.receive_text()
            await websocket.send_text(f"Echo: {data}")
        except WebSocketDisconnect:
            connected = False


def _create_ws_echo_backend() -> FastAPI:
    """Create a WebSocket echo server with a chat UI."""
    app = FastAPI()
    app.add_api_route("/", _ws_echo_root)
    app.add_api_websocket_route("/ws", _ws_echo_handler)
    return app


def _run_backend(app: FastAPI, port: int) -> None:
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


def _write_line(message: str) -> None:
    """Write a line to stdout."""
    sys.stdout.write(message + "\n")
    sys.stdout.flush()


def main() -> None:
    forwarding_port = 8420

    # Create temp dir for auth data and a sample file tree
    data_dir = Path(tempfile.mkdtemp(prefix="changelings-demo-"))
    auth_store = FileAuthStore(data_directory=data_dir / "auth")

    # Create sample files for the file browser
    sample_dir = data_dir / "sample-files"
    sample_dir.mkdir()
    (sample_dir / "hello.txt").write_text("Hello from the file browser!\nThis file is served through the proxy.\n")
    (sample_dir / "readme.md").write_text(
        "# Sample Project\n\nThis directory is being served by the file-browser changeling.\n"
    )
    sub_dir = sample_dir / "src"
    sub_dir.mkdir()
    (sub_dir / "main.py").write_text(
        "def main():\n    print('Hello, world!')\n\nif __name__ == '__main__':\n    main()\n"
    )

    # Set up backends
    file_browser_id = AgentId()
    ws_echo_id = AgentId()

    backend_configs: list[tuple[str, AgentId, int, FastAPI]] = [
        ("file-browser", file_browser_id, 9001, _create_file_browser_backend(str(sample_dir))),
        ("ws-echo", ws_echo_id, 9002, _create_ws_echo_backend()),
    ]

    # Start backend servers
    url_by_agent_and_server: dict[str, dict[str, str]] = {}
    for label, agent_id, port, backend_app in backend_configs:
        thread = threading.Thread(
            target=_run_backend,
            args=(backend_app, port),
            daemon=True,
        )
        thread.start()
        url_by_agent_and_server[str(agent_id)] = {label: f"http://127.0.0.1:{port}"}

    backend_resolver = StaticBackendResolver(url_by_agent_and_server=url_by_agent_and_server)

    _write_line("")
    _write_line("=" * 60)
    _write_line("Changelings Forwarding Server Demo")
    _write_line("=" * 60)
    _write_line("")
    _write_line(f"Data directory: {data_dir}")
    _write_line("")

    # Generate one-time codes and display login URLs
    for label, agent_id, port, _ in backend_configs:
        code = OneTimeCode(secrets.token_urlsafe(32))
        auth_store.add_one_time_code(
            agent_id=agent_id,
            code=code,
        )
        login_url = f"http://127.0.0.1:{forwarding_port}/login?agent_id={agent_id}&one_time_code={code}"
        _write_line(f"Login URL for {label} ({agent_id}, port {port}):")
        _write_line(f"  {login_url}")
        _write_line("")

    _write_line(f"Landing page: http://127.0.0.1:{forwarding_port}/")
    _write_line("")
    _write_line("What to test:")
    _write_line("  1. Open the file-browser login URL -- browse files, click into subdirs")
    _write_line("  2. Open the ws-echo login URL -- send messages, see echoed responses")
    _write_line("  3. Visit the landing page to see both agents listed")
    _write_line("  4. Try accessing /agents/<agent_id>/ without auth (should get 403)")
    _write_line("")
    _write_line("Press Ctrl+C to stop.")
    _write_line("=" * 60)
    _write_line("")

    # Start forwarding server
    app = create_forwarding_server(
        auth_store=auth_store,
        backend_resolver=backend_resolver,
        http_client=None,
    )
    uvicorn.run(app, host="127.0.0.1", port=forwarding_port)


if __name__ == "__main__":
    main()
