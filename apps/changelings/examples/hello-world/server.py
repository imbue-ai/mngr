#!/usr/bin/env python3
"""Simple HTTP server for the hello-world changeling.

Serves a basic web page with some interactive elements to demonstrate
that the forwarding server is working correctly.

Reads the PORT environment variable (default: 9100).
"""

import json
import os
import sys
from http.server import BaseHTTPRequestHandler
from http.server import HTTPServer
from urllib.parse import unquote

_DEFAULT_PORT = 9100


_INDEX_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>Hello World Changeling</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body {
      font-family: system-ui, -apple-system, sans-serif;
      padding: 40px;
      max-width: 700px;
      margin: 0 auto;
      background: whitesmoke;
      color: rgb(30, 30, 30);
    }
    h1 { margin-bottom: 16px; }
    .status {
      background: rgb(230, 255, 230);
      border: 1px solid rgb(180, 230, 180);
      border-radius: 6px;
      padding: 16px;
      margin-bottom: 24px;
    }
    .info { color: gray; font-size: 14px; margin-top: 24px; }
    .info code { background: rgb(238, 238, 238); padding: 2px 6px; border-radius: 3px; }
    form { margin-top: 24px; }
    input[type=text] {
      padding: 8px 12px; font-size: 14px;
      border: 1px solid rgb(204, 204, 204); border-radius: 4px;
      width: 300px;
    }
    button {
      padding: 8px 16px; font-size: 14px;
      cursor: pointer; border-radius: 4px;
      border: 1px solid rgb(204, 204, 204);
    }
    #echo-result {
      margin-top: 12px; padding: 12px;
      background: rgb(250, 250, 250);
      border: 1px solid rgb(238, 238, 238);
      border-radius: 4px;
      font-family: monospace;
      min-height: 40px;
    }
  </style>
</head>
<body>
  <h1>Hello World Changeling</h1>
  <div class="status">
    This changeling is running and serving HTTP traffic.
    If you can see this page through the forwarding server,
    the proxy is working correctly.
  </div>

  <h3>Echo Test</h3>
  <p>Type something and press Echo to verify round-trip HTTP works:</p>
  <form id="echo-form" action="/echo" method="GET">
    <input type="text" name="message" id="message-input" placeholder="Type a message...">
    <button type="submit">Echo</button>
  </form>
  <div id="echo-result"></div>

  <div class="info">
    <p>Server port: <code>PORT_PLACEHOLDER</code></p>
    <p>Browser path: <code id="browser-path"></code></p>
  </div>
  <script>
    document.getElementById('browser-path').textContent = window.location.pathname;

    document.getElementById('echo-form').addEventListener('submit', function(e) {
      e.preventDefault();
      var msg = document.getElementById('message-input').value;
      fetch('/echo?message=' + encodeURIComponent(msg))
        .then(function(r) { return r.text(); })
        .then(function(text) {
          document.getElementById('echo-result').textContent = text;
        });
    });
  </script>
</body>
</html>"""


class _Handler(BaseHTTPRequestHandler):
    """Simple HTTP request handler for the hello-world changeling."""

    def do_GET(self) -> None:
        if self.path == "/" or self.path == "":
            self._serve_index()
        elif self.path.startswith("/echo"):
            self._serve_echo()
        elif self.path == "/health":
            self._serve_health()
        else:
            self._serve_not_found()

    def _serve_index(self) -> None:
        port = os.environ.get("PORT", str(_DEFAULT_PORT))
        html = _INDEX_HTML.replace("PORT_PLACEHOLDER", port)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def _serve_echo(self) -> None:
        message = ""
        if "?" in self.path:
            query = self.path.split("?", 1)[1]
            for param in query.split("&"):
                if param.startswith("message="):
                    message = param[8:]
                    break
        message = unquote(message)
        response = "Echo: {}".format(message) if message else "Echo: (empty)"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(response.encode())

    def _serve_health(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def _serve_not_found(self) -> None:
        self.send_response(404)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Not found")

    def log_message(self, format: str, *args: object) -> None:
        """Suppress default access log output."""


def _write_server_log(port: int) -> None:
    """Write a server log record so the forwarding server can discover this agent.

    Writes to $MNG_AGENT_STATE_DIR/logs/servers.jsonl following the convention
    that agents self-report their running servers.
    """
    agent_state_dir = os.environ.get("MNG_AGENT_STATE_DIR")
    if not agent_state_dir:
        return
    logs_dir = os.path.join(agent_state_dir, "logs")
    os.makedirs(logs_dir, exist_ok=True)
    record = {"server": "web", "url": "http://127.0.0.1:{}".format(port)}
    with open(os.path.join(logs_dir, "servers.jsonl"), "a") as f:
        f.write(json.dumps(record) + "\n")


def main() -> None:
    port = int(os.environ.get("PORT", str(_DEFAULT_PORT)))
    http_server = HTTPServer(("0.0.0.0", port), _Handler)
    _write_server_log(port)
    sys.stderr.write("hello-world changeling serving on port {}\n".format(port))
    sys.stderr.flush()
    http_server.serve_forever()


if __name__ == "__main__":
    main()
