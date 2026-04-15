import os
from collections.abc import Sequence
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServerName
from imbue.mngr.primitives import AgentId

_JINJA_ENV: Final[Environment] = Environment(autoescape=select_autoescape(default=True))

_COMMON_STYLES: Final[str] = """
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; padding: 40px; background: whitesmoke; }
    h1 { margin-bottom: 24px; color: rgb(26, 26, 46); }
    .btn {
      display: inline-block; padding: 12px 20px;
      background: rgb(26, 26, 46); color: white; text-decoration: none;
      border-radius: 6px; font-size: 16px; border: none; cursor: pointer;
    }
    .btn:hover { background: rgb(42, 42, 78); }
"""

_LANDING_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Workspaces</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    body { background: #f8fafc; padding: 0; font-size: 14px; }
    .page { max-width: 800px; margin: 0 auto; padding: 48px 0; }
    .create-btn {
      padding: 6px 16px; background: #1e293b; color: white; border: none;
      border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer;
      text-decoration: none; display: inline-block; font-family: inherit;
    }
    .create-btn:hover { background: #334155; }
    table { width: 100%; border-collapse: collapse; }
    thead th {
      text-align: left; padding: 10px 16px; font-size: 14px; font-weight: 400;
      color: #94a3b8; border-bottom: 1px solid #e2e8f0;
    }
    thead th:last-child { text-align: right; }
    tbody tr { cursor: pointer; transition: background 0.1s; }
    tbody tr:hover { background: #f1f5f9; }
    tbody td {
      padding: 20px 16px; font-size: 14px; color: #334155;
      border-bottom: 1px solid #f1f5f9; vertical-align: middle;
    }
    tbody td:last-child { text-align: right; }
    .ws-name { font-weight: 500; color: #0f172a; }
    .shared-with { color: #94a3b8; }
    .menu-wrapper { position: relative; display: inline-block; }
    .menu-btn {
      background: none; border: 1px solid transparent; border-radius: 4px;
      cursor: pointer; padding: 4px 6px; color: #94a3b8; line-height: 1;
      display: flex; align-items: center;
    }
    .menu-btn:hover { background: #e2e8f0; border-color: #cbd5e1; color: #64748b; }
    .menu-btn svg { width: 16px; height: 16px; }
    .menu-dropdown {
      display: none; position: absolute; right: 0; top: 100%; margin-top: 4px;
      background: white; border: 1px solid #e2e8f0; border-radius: 6px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.08); min-width: 160px; z-index: 10;
      padding: 4px 0;
    }
    .menu-dropdown.open { display: block; }
    .menu-item {
      display: block; width: 100%; padding: 8px 14px; font-size: 13px;
      text-align: left; background: none; border: none; cursor: pointer;
      color: #334155;
    }
    .menu-item:hover { background: #f1f5f9; }
    .menu-item.destructive { color: #dc2626; }
    .menu-item.destructive:hover { background: #fef2f2; }
    .empty-state { color: #94a3b8; font-size: 15px; text-align: center; padding: 48px 0; }
  </style>
</head>
<body>
  <div class="page">
    {% if agent_ids %}
    <table>
      <thead>
        <tr>
          <th>Name</th>
          <th>Shared with</th>
          <th style="text-align: right;"><a href="/create" class="create-btn">Create</a></th>
        </tr>
      </thead>
      <tbody>
        {% for agent_id in agent_ids %}
        <tr onclick="window.location='/forwarding/{{ agent_id }}/'" data-agent-id="{{ agent_id }}">
          <td><span class="ws-name">{{ agent_names.get(agent_id | string, agent_id) }}</span></td>
          <td><span class="shared-with">No one</span></td>
          <td>
            <button class="menu-btn" onclick="event.stopPropagation(); window.location='/workspace/{{ agent_id }}/settings'" title="Settings">
              <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            </button>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
    <script></script>
    {% else %}
      {% if is_discovering %}
    <div style="display: flex; align-items: center; justify-content: center; min-height: 80vh;">
      <p class="empty-state" style="padding: 0;">Discovering agents...</p>
    </div>
    <script>setTimeout(function() { location.reload(); }, 2000);</script>
      {% else %}
    <div style="text-align: center; padding: 48px 0;">
      <p class="empty-state" style="margin-bottom: 24px;">No workspaces yet</p>
      <a href="/create" class="create-btn">Create</a>
    </div>
      {% endif %}
    {% endif %}
  </div>
</body>
</html>"""
)

_CREATE_FORM_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Create a Workspace</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    body { background: #f8fafc; padding: 0; font-size: 14px; }
    .page { max-width: 800px; margin: 0 auto; padding: 48px 16px; }
    .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 32px; padding-top: 10px; }
    .page-header a { color: #64748b; text-decoration: none; font-size: 14px; }
    .page-header a:hover { color: #334155; }
    .submit-btn {
      padding: 6px 16px; background: #1e293b; color: white; border: none;
      border-radius: 6px; font-size: 14px; font-weight: 600; cursor: pointer;
      font-family: inherit;
    }
    .submit-btn:hover { background: #334155; }
    .form-group { display: flex; gap: 24px; margin-bottom: 16px; align-items: flex-start; }
    .form-label { flex: 0 0 200px; padding-top: 10px; }
    .form-label label { font-size: 14px; color: #334155; font-weight: 500; display: block; }
    .form-label .help-text { margin-top: 2px; font-size: 13px; color: #94a3b8; }
    .form-input { flex: 1; }
    input[type="text"], select {
      width: 100%; padding: 10px 12px;
      border: 1px solid #e2e8f0; border-radius: 6px; font-size: 14px;
      font-family: inherit; background: white; color: #0f172a;
    }
    input[type="text"]:focus, select:focus { outline: none; border-color: #94a3b8; }
  </style>
</head>
<body>
  <div class="page">
    <div class="page-header">
      <a href="/">Back to workspace list</a>
      <button type="submit" form="create-form" class="submit-btn">Create</button>
    </div>
    <form id="create-form" action="/create" method="post">
      <div class="form-group">
        <div class="form-label">
          <label for="agent_name">Name</label>
        </div>
        <div class="form-input">
          <input type="text" id="agent_name" name="agent_name" value="{{ agent_name }}"
                 placeholder="selene" required>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="git_url">Repository</label>
          <p class="help-text">Git URL or local path</p>
        </div>
        <div class="form-input">
          <input type="text" id="git_url" name="git_url" value="{{ git_url }}"
                 placeholder="https://github.com/user/repo.git" required>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="branch">Branch</label>
          <p class="help-text">Leave empty for default</p>
        </div>
        <div class="form-input">
          <input type="text" id="branch" name="branch" value="{{ branch }}"
                 placeholder="main">
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="launch_mode">Launch mode</label>
          <p class="help-text">Local: Docker. Dev: this host.</p>
        </div>
        <div class="form-input">
          <select id="launch_mode" name="launch_mode">
            {% for mode in launch_modes %}
            <option value="{{ mode.value }}"{% if mode.value == selected_launch_mode %} selected{% endif %}>{{ mode.value | lower }}</option>
            {% endfor %}
          </select>
        </div>
      </div>
    </form>
  </div>
</body>
</html>"""
)

_CREATING_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Creating your workspace...</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .status { margin-top: 16px; font-size: 16px; color: rgb(60, 60, 80); }
    .error { margin-top: 16px; color: darkred; }
    .spinner {
      display: inline-block; width: 20px; height: 20px;
      border: 3px solid rgb(200, 200, 210); border-top: 3px solid rgb(26, 26, 46);
      border-radius: 50%; animation: spin 1s linear infinite;
      vertical-align: middle; margin-right: 8px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    #logs {
      margin-top: 16px; padding: 12px; background: rgb(26, 26, 46); color: rgb(200, 210, 220);
      font-family: monospace; font-size: 13px; border-radius: 6px;
      max-height: 400px; overflow-y: auto; white-space: pre-wrap;
    }
  </style>
</head>
<body>
  <h1>Creating your workspace...</h1>
  <p class="status" id="status"><span class="spinner"></span> {{ status_text }}</p>
  <div id="logs"></div>
  <script>
    const agentId = '{{ agent_id }}';
    const logsEl = document.getElementById('logs');
    const statusEl = document.getElementById('status');
    const source = new EventSource('/api/create-agent/' + agentId + '/logs');

    var pendingLines = [];
    var flushScheduled = false;

    function flushLogs() {
      flushScheduled = false;
      if (pendingLines.length === 0) return;
      var fragment = document.createDocumentFragment();
      fragment.appendChild(document.createTextNode(pendingLines.join('\\n') + '\\n'));
      pendingLines = [];
      logsEl.appendChild(fragment);
      logsEl.scrollTop = logsEl.scrollHeight;
    }

    source.onmessage = function(event) {
      try {
        var data = JSON.parse(event.data);
        if (data._type === 'done') {
          source.close();
          flushLogs();
          if (data.status === 'DONE' && data.redirect_url) {
            statusEl.textContent = 'Done! Redirecting...';
            window.location.href = data.redirect_url;
          } else if (data.status === 'FAILED') {
            statusEl.textContent = 'Failed: ' + (data.error || 'unknown error');
            statusEl.classList.add('error');
          }
        } else if (data.log) {
          pendingLines.push(data.log);
          if (!flushScheduled) {
            flushScheduled = true;
            requestAnimationFrame(flushLogs);
          }
        }
      } catch(e) {
        // Ignore parse errors for keepalive comments
      }
    };

    source.onerror = function() {
      source.close();
    };
  </script>
</body>
</html>"""
)

_LOGIN_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Login - Workspaces</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .login-message { color: gray; font-size: 16px; }
  </style>
</head>
<body>
  <h1>Workspaces</h1>
  <p class="login-message">
    Please use the login URL printed in the terminal where the server is running.
  </p>
</body>
</html>"""
)

_LOGIN_REDIRECT_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head><title>Authenticating...</title></head>
<body>
<p>Authenticating...</p>
<script>
window.location.href = '/authenticate?one_time_code={{ one_time_code }}';
</script>
</body>
</html>"""

_AUTH_ERROR_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
  <title>Authentication Error</title>
  <style>
    body { font-family: system-ui, -apple-system, sans-serif; padding: 40px; background: whitesmoke; }
    .error { background: rgb(255, 238, 238); border: 1px solid rgb(255, 204, 204); padding: 20px; border-radius: 6px; color: darkred; }
  </style>
</head>
<body>
  <div class="error">
    <h2>Authentication Failed</h2>
    <p>{{ message }}</p>
    <p>Each login URL can only be used once. Please use the login URL printed in the terminal where the server is running, or restart the server to generate a new one.</p>
  </div>
</body>
</html>"""


@pure
def render_landing_page(
    accessible_agent_ids: Sequence[AgentId],
    telegram_status_by_agent_id: dict[str, bool] | None = None,
    is_discovering: bool = False,
    agent_names: dict[str, str] | None = None,
) -> str:
    """Render the landing page listing accessible workspaces.

    telegram_status_by_agent_id maps agent ID strings to whether they have
    active Telegram bot credentials. When None, no telegram buttons are shown.

    agent_names maps agent ID strings to human-readable workspace names.

    When is_discovering is True, the page shows a "Discovering agents..." message
    with auto-refresh instead of the empty state. This is used when the stream
    manager hasn't completed initial agent discovery yet.
    """
    template = _JINJA_ENV.from_string(_LANDING_PAGE_TEMPLATE)
    return template.render(
        agent_ids=accessible_agent_ids,
        telegram_enabled=telegram_status_by_agent_id is not None,
        telegram_status_by_agent_id=telegram_status_by_agent_id or {},
        is_discovering=is_discovering,
        agent_names=agent_names or {},
    )


_DEFAULT_GIT_URL: Final[str] = os.getenv(
    "MINDS_WORKSPACE_GIT_URL", "https://github.com/imbue-ai/forever-claude-template.git"
)


_DEFAULT_AGENT_NAME: Final[str] = os.getenv("MINDS_WORKSPACE_NAME", "selene")


_DEFAULT_BRANCH: Final[str] = os.getenv("MINDS_WORKSPACE_BRANCH", "main")


@pure
def render_create_form(
    git_url: str = "",
    agent_name: str = "",
    branch: str = "",
    launch_mode: LaunchMode = LaunchMode.LOCAL,
) -> str:
    """Render the agent creation form page.

    When git_url is provided, the form field is pre-filled with that value.
    Defaults to the forever-claude-template repository URL when empty.
    """
    effective_url = git_url if git_url else _DEFAULT_GIT_URL
    effective_name = agent_name if agent_name else _DEFAULT_AGENT_NAME
    effective_branch = branch if branch else _DEFAULT_BRANCH
    template = _JINJA_ENV.from_string(_CREATE_FORM_TEMPLATE)
    return template.render(
        git_url=effective_url,
        agent_name=effective_name,
        branch=effective_branch,
        launch_modes=list(LaunchMode),
        selected_launch_mode=launch_mode.value,
    )


@pure
def render_creating_page(agent_id: AgentId, info: AgentCreationInfo) -> str:
    """Render the progress page shown while an agent is being created.

    The page streams logs from /api/create-agent/{agent_id}/logs via SSE
    and auto-redirects to the agent when creation completes.
    """
    status_text_map = {
        "CLONING": "Cloning repository...",
        "CREATING": "Creating agent...",
        "DONE": "Done! Redirecting...",
        "FAILED": "Failed: {}".format(info.error or "unknown error"),
    }
    status_text = status_text_map.get(str(info.status), "Working...")
    template = _JINJA_ENV.from_string(_CREATING_PAGE_TEMPLATE)
    return template.render(agent_id=agent_id, status_text=status_text)


@pure
def render_login_page() -> str:
    """Render the login prompt page for unauthenticated users."""
    template = _JINJA_ENV.from_string(_LOGIN_PAGE_TEMPLATE)
    return template.render()


@pure
def render_login_redirect_page(
    one_time_code: OneTimeCode,
) -> str:
    """Render the JS redirect page that forwards to /authenticate."""
    template = _JINJA_ENV.from_string(_LOGIN_REDIRECT_TEMPLATE)
    return template.render(one_time_code=one_time_code)


@pure
def render_auth_error_page(message: str) -> str:
    """Render an error page for failed authentication."""
    template = _JINJA_ENV.from_string(_AUTH_ERROR_TEMPLATE)
    return template.render(message=message)


_AGENT_SERVERS_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
  <title>Servers - {{ agent_id }}</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; padding: 40px; background: whitesmoke; }
    h1 { margin-bottom: 8px; color: rgb(26, 26, 46); }
    .subtitle { margin-bottom: 24px; color: gray; font-size: 14px; }
    .server-list { list-style: none; }
    .server-list li {
      margin-bottom: 12px; padding: 12px 16px;
      background: white; border: 1px solid rgb(220, 220, 230); border-radius: 8px;
      display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    }
    .server-name {
      font-weight: bold; font-size: 16px; color: rgb(26, 26, 46);
      min-width: 100px;
    }
    .server-links { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
    .server-links a {
      display: inline-block; padding: 6px 12px;
      background: rgb(26, 26, 46); color: white; text-decoration: none;
      border-radius: 4px; font-size: 13px;
    }
    .server-links a:hover { background: rgb(42, 42, 78); }
    .server-links a.global-link {
      background: rgb(37, 99, 235);
    }
    .server-links a.global-link:hover { background: rgb(29, 78, 216); }
    .toggle-btn {
      padding: 4px 10px; border-radius: 4px; font-size: 12px;
      border: 1px solid rgb(200, 200, 210); cursor: pointer;
      background: white; color: rgb(60, 60, 80);
    }
    .toggle-btn:hover { background: rgb(240, 240, 245); }
    .toggle-btn.enabled { background: rgb(220, 252, 231); border-color: rgb(134, 239, 172); color: rgb(22, 101, 52); }
    .empty-state { color: gray; font-size: 16px; }
    .back-link { margin-top: 24px; }
    .back-link a { color: rgb(26, 26, 46); text-decoration: underline; }
  </style>
</head>
<body>
  <h1>{{ agent_id }}</h1>
  <p class="subtitle">Available servers</p>
  {% if server_names %}
  <ul class="server-list">
    {% for server_name in server_names %}
    <li>
      <span class="server-name">{{ server_name }}</span>
      <div class="server-links">
        <a href="/forwarding/{{ agent_id }}/{{ server_name }}/">Local</a>
        {% if cf_services and server_name in cf_services %}
        <a href="https://{{ cf_services[server_name] }}" class="global-link" target="_blank">Global</a>
        <button class="toggle-btn enabled" onclick="toggleGlobal('{{ agent_id }}', '{{ server_name }}', false)">Disable global</button>
        {% else %}
        <button class="toggle-btn" onclick="toggleGlobal('{{ agent_id }}', '{{ server_name }}', true)">Enable global</button>
        {% endif %}
      </div>
    </li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty-state">
    No servers are currently running for this agent.
  </p>
  {% endif %}
  <div class="back-link"><a href="/">Back to all workspaces</a></div>
  <script>
  async function toggleGlobal(agentId, serverName, enable) {
    try {
      const resp = await fetch('/forwarding/' + agentId + '/servers/' + serverName + '/global', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({enabled: enable})
      });
      if (resp.ok) {
        window.location.reload();
      } else {
        const data = await resp.json();
        alert('Failed: ' + (data.error || resp.statusText));
      }
    } catch (e) {
      alert('Failed: ' + e.message);
    }
  }
  </script>
</body>
</html>"""


@pure
def render_agent_servers_page(
    agent_id: AgentId,
    server_names: Sequence[ServerName],
    cf_services: dict[str, str] | None = None,
) -> str:
    """Render a page listing all available servers for a specific agent.

    cf_services maps server names to their cloudflare hostnames (if globally forwarded).
    """
    template = _JINJA_ENV.from_string(_AGENT_SERVERS_TEMPLATE)
    return template.render(agent_id=agent_id, server_names=server_names, cf_services=cf_services or {})


# -- Chrome (persistent shell) templates --

_CHROME_TITLEBAR_HEIGHT: Final[int] = 38

_CHROME_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Minds</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
html, body { height: 100%; overflow: hidden; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0f172a;
}

#minds-titlebar {
  position: fixed; top: 0; left: 0; right: 0;
  height: """ + str(_CHROME_TITLEBAR_HEIGHT) + """px;
  background: #1e293b;
  display: flex; align-items: center;
  user-select: none;
  -webkit-app-region: drag;
  z-index: 100;
  border-bottom: 1px solid #334155;
  padding: 0 4px;
}
{% if is_mac %}#minds-titlebar { padding-left: 72px; }{% endif %}

#minds-titlebar button {
  -webkit-app-region: no-drag;
  background: none; border: none; color: #94a3b8; cursor: pointer;
  width: 32px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border-radius: 4px; font-size: 14px; line-height: 1;
}
#minds-titlebar button:hover { color: #e2e8f0; background: rgba(255,255,255,0.08); }
#minds-titlebar button:active { background: rgba(255,255,255,0.12); }
#minds-titlebar svg {
  width: 16px; height: 16px; fill: none; stroke: currentColor;
  stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
}

.minds-nav { display: flex; gap: 2px; }
.minds-title {
  flex: 1; color: #cbd5e1; font-size: 12px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  text-align: center; padding: 0 8px;
}

.minds-user-area { position: relative; -webkit-app-region: no-drag; flex-shrink: 0; }
.minds-user-btn {
  width: auto !important; height: auto !important; display: inline-block !important;
  color: #94a3b8; cursor: pointer; padding: 4px 10px; border-radius: 4px;
  font-size: 12px; font-family: inherit; white-space: nowrap;
}
.minds-user-btn:hover { background: rgba(255,255,255,0.08); color: #e2e8f0; }

.minds-wc { display: flex; }
{% if is_mac %}.minds-wc { display: none; }{% endif %}
.minds-wc button { border-radius: 0; width: 36px; height: """ + str(_CHROME_TITLEBAR_HEIGHT) + """px; }
.minds-wc button:hover { background: rgba(255,255,255,0.08); border-radius: 0; }
.minds-wc button:last-child:hover { background: rgb(220, 38, 38); color: white; border-radius: 0; }

/* Sidebar (browser mode) */
#sidebar-panel {
  position: fixed; left: 0; top: """ + str(_CHROME_TITLEBAR_HEIGHT) + """px;
  width: 260px; height: calc(100% - """ + str(_CHROME_TITLEBAR_HEIGHT) + """px);
  background: #f3f2ef; z-index: 50;
  box-shadow: 4px 0 12px rgba(0,0,0,0.15);
  transform: translateX(-100%);
  transition: transform 200ms ease-in-out;
  overflow-y: auto;
  padding: 12px 0;
}
#sidebar-panel.sidebar-visible { transform: translateX(0); }

.sidebar-item {
  padding: 10px 16px; cursor: pointer; font-size: 13px; font-weight: 500;
  color: #37352f; border-radius: 6px; margin: 2px 8px;
  transition: background 100ms;
}
.sidebar-item:hover { background: #edecea; }

.sidebar-empty {
  padding: 24px 16px; font-size: 13px; color: #787774; text-align: center;
}

/* Content area (browser mode) */
#content-frame {
  position: fixed; left: 0; top: """ + str(_CHROME_TITLEBAR_HEIGHT) + """px;
  width: 100%; height: calc(100% - """ + str(_CHROME_TITLEBAR_HEIGHT) + """px);
  border: none;
}
</style>
</head>
<body>
<div id="minds-titlebar">
  <div class="minds-nav">
    <button id="sidebar-toggle" title="Workspaces">
      <svg viewBox="0 0 24 24"><rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/></svg>
    </button>
    <button id="home-btn" title="Home">
      <svg viewBox="0 0 24 24"><path d="M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-4 0h4"/></svg>
    </button>
    <button id="back-btn" title="Back">
      <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    <button id="forward-btn" title="Forward">
      <svg viewBox="0 0 24 24"><polyline points="9 6 15 12 9 18"/></svg>
    </button>
  </div>
  <span class="minds-title" id="page-title">Minds</span>
  <div class="minds-user-area">
    <button id="user-btn" class="minds-user-btn" title="Account">Log in</button>
  </div>
  <button id="requests-toggle" title="Requests" style="position:relative;">
    <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>
    <span id="requests-badge" style="display:none;position:absolute;top:2px;right:2px;width:8px;height:8px;border-radius:50%;background:#ef4444;"></span>
  </button>
  <div class="minds-wc">
    <button id="min-btn" title="Minimize">
      <svg viewBox="0 0 12 12" style="width:12px;height:12px"><line x1="2" y1="6" x2="10" y2="6"/></svg>
    </button>
    <button id="max-btn" title="Maximize">
      <svg viewBox="0 0 12 12" style="width:12px;height:12px"><rect x="2" y="2" width="8" height="8" rx="0.5"/></svg>
    </button>
    <button id="close-btn" title="Close">
      <svg viewBox="0 0 12 12" style="width:12px;height:12px"><line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/></svg>
    </button>
  </div>
</div>

<!-- Sidebar panel (used in browser mode; hidden by default) -->
<div id="sidebar-panel">
  <div id="sidebar-workspaces">
    <div class="sidebar-empty">No workspaces</div>
  </div>
</div>

<!-- Content iframe (browser mode only, hidden in Electron) -->
<iframe id="content-frame" src="/"></iframe>

<script>
var isElectron = !!window.minds;

// -- Navigation adapter --
function navigateContent(url) {
  if (isElectron) window.minds.navigateContent(url);
  else document.getElementById('content-frame').src = url;
}
function goBack() {
  if (isElectron) window.minds.contentGoBack();
  else { try { document.getElementById('content-frame').contentWindow.history.back(); } catch(e) {} }
}
function goForward() {
  if (isElectron) window.minds.contentGoForward();
  else { try { document.getElementById('content-frame').contentWindow.history.forward(); } catch(e) {} }
}

// -- Sidebar toggle --
var sidebarOpen = false;
function toggleSidebar() {
  if (isElectron) {
    window.minds.toggleSidebar();
    sidebarOpen = !sidebarOpen;
  } else {
    var panel = document.getElementById('sidebar-panel');
    sidebarOpen = !sidebarOpen;
    if (sidebarOpen) panel.classList.add('sidebar-visible');
    else panel.classList.remove('sidebar-visible');
  }
}

function selectWorkspace(agentId) {
  navigateContent('/forwarding/' + agentId + '/');
  // Close sidebar. In Electron, navigate-content already removes the sidebar
  // WebContentsView on the main process side, so only reset the local state flag
  // without sending another toggle-sidebar IPC (which would re-create it).
  if (isElectron) {
    sidebarOpen = false;
  } else {
    sidebarOpen = false;
    document.getElementById('sidebar-panel').classList.remove('sidebar-visible');
  }
}

// -- Button handlers --
document.getElementById('sidebar-toggle').onclick = toggleSidebar;
document.getElementById('home-btn').onclick = function() { navigateContent('/'); };
document.getElementById('back-btn').onclick = goBack;
document.getElementById('forward-btn').onclick = goForward;

// Window controls (Electron only)
if (isElectron) {
  document.getElementById('min-btn').onclick = function() { window.minds.minimize(); };
  document.getElementById('max-btn').onclick = function() { window.minds.maximize(); };
  document.getElementById('close-btn').onclick = function() { window.minds.close(); };
  // Hide iframe in Electron (content is in WebContentsView)
  document.getElementById('content-frame').style.display = 'none';
  // Hide browser sidebar panel in Electron (separate WebContentsView)
  document.getElementById('sidebar-panel').style.display = 'none';
}

// -- Title tracking + auth refresh on navigation --
function refreshAuthStatus() {
  fetch('/auth/api/status').then(function(r) { return r.json(); }).then(updateAuthUI).catch(function() {});
}

if (isElectron) {
  window.minds.onContentTitleChange(function(title) {
    document.getElementById('page-title').textContent = title || 'Minds';
  });
  window.minds.onContentURLChange(function() {
    refreshAuthStatus();
  });
} else {
  setInterval(function() {
    try {
      var t = document.getElementById('content-frame').contentDocument.title;
      if (t) document.getElementById('page-title').textContent = t;
    } catch(e) {}
  }, 500);
  // Re-check auth on iframe navigation
  document.getElementById('content-frame').addEventListener('load', refreshAuthStatus);
}

// -- Auth status --
var signedIn = false;
function updateAuthUI(data) {
  var btn = document.getElementById('user-btn');
  if (data.signedIn) {
    signedIn = true;
    btn.textContent = 'Manage account(s)';
    btn.title = data.email || 'Manage accounts';
  } else {
    signedIn = false;
    btn.textContent = 'Log in';
    btn.title = 'Sign in to your account';
  }
}
refreshAuthStatus();

document.getElementById('user-btn').onclick = function() {
  if (signedIn) navigateContent('/accounts');
  else navigateContent('/auth/login');
};

// -- Requests panel toggle --
document.getElementById('requests-toggle').onclick = function() {
  if (isElectron) window.minds.toggleRequestsPanel();
};

// -- SSE for workspace list (browser mode sidebar) --
function renderWorkspaces(workspaces) {
  var container = document.getElementById('sidebar-workspaces');
  if (!workspaces || workspaces.length === 0) {
    container.innerHTML = '<div class="sidebar-empty">No workspaces</div>';
    return;
  }
  // Group by account
  var groups = {};
  workspaces.forEach(function(w) {
    var key = w.account || 'Private';
    if (!groups[key]) groups[key] = [];
    groups[key].push(w);
  });
  // Render with Private first, then alphabetical
  var keys = Object.keys(groups).sort(function(a, b) {
    if (a === 'Private') return -1;
    if (b === 'Private') return 1;
    return a.localeCompare(b);
  });
  var html = '';
  keys.forEach(function(key) {
    html += '<div style="padding:6px 16px 2px;font-size:11px;color:#787774;text-transform:uppercase;letter-spacing:0.5px;">' + key + '</div>';
    groups[key].forEach(function(w) {
      html += '<div class="sidebar-item" onclick="selectWorkspace(\\'' + w.id + '\\')">' + (w.name || w.id) + '</div>';
    });
  });
  container.innerHTML = html;
}

function updateRequestsBadge(count) {
  var badge = document.getElementById('requests-badge');
  if (badge) badge.style.display = count > 0 ? 'block' : 'none';
}

var evtSource = null;
function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/_chrome/events');
  evtSource.onmessage = function(event) {
    try {
      var data = JSON.parse(event.data);
      if (data.type === 'workspaces') renderWorkspaces(data.workspaces);
      if (data.type === 'auth_status') updateAuthUI(data);
      if (data.type === 'request_count') updateRequestsBadge(data.count);
    } catch(e) {}
  };
  evtSource.onerror = function() {
    evtSource.close();
    evtSource = null;
    setTimeout(connectSSE, 5000);
  };
}
connectSSE();
</script>
</body>
</html>"""


_SIDEBAR_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Workspaces</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #f3f2ef;
  overflow-y: auto;
  padding: 12px 0;
}

.sidebar-item {
  padding: 10px 16px; cursor: pointer; font-size: 13px; font-weight: 500;
  color: #37352f; border-radius: 6px; margin: 2px 8px;
  transition: background 100ms;
}
.sidebar-item:hover { background: #edecea; }

.sidebar-empty {
  padding: 24px 16px; font-size: 13px; color: #787774; text-align: center;
}
</style>
</head>
<body>
<div id="sidebar-workspaces">
  <div class="sidebar-empty">No workspaces</div>
</div>
<script>
var isElectron = !!window.minds;

function selectWorkspace(agentId) {
  if (isElectron) window.minds.navigateContent('/forwarding/' + agentId + '/');
}

function renderWorkspaces(workspaces) {
  var container = document.getElementById('sidebar-workspaces');
  if (!workspaces || workspaces.length === 0) {
    container.innerHTML = '<div class="sidebar-empty">No workspaces</div>';
    return;
  }
  var groups = {};
  workspaces.forEach(function(w) {
    var key = w.account || 'Private';
    if (!groups[key]) groups[key] = [];
    groups[key].push(w);
  });
  var keys = Object.keys(groups).sort(function(a, b) {
    if (a === 'Private') return -1;
    if (b === 'Private') return 1;
    return a.localeCompare(b);
  });
  var html = '';
  keys.forEach(function(key) {
    html += '<div style="padding:6px 16px 2px;font-size:11px;color:#787774;text-transform:uppercase;letter-spacing:0.5px;">' + key + '</div>';
    groups[key].forEach(function(w) {
      html += '<div class="sidebar-item" onclick="selectWorkspace(\\'' + w.id + '\\')">' + (w.name || w.id) + '</div>';
    });
  });
  container.innerHTML = html;
}

var evtSource = null;
function connectSSE() {
  if (evtSource) evtSource.close();
  evtSource = new EventSource('/_chrome/events');
  evtSource.onmessage = function(event) {
    try {
      var data = JSON.parse(event.data);
      if (data.type === 'workspaces') renderWorkspaces(data.workspaces);
    } catch(e) {}
  };
  evtSource.onerror = function() {
    evtSource.close();
    evtSource = null;
    setTimeout(connectSSE, 5000);
  };
}
connectSSE();
</script>
</body>
</html>"""


@pure
def render_chrome_page(
    is_mac: bool = False,
    is_authenticated: bool = False,
    initial_workspaces: Sequence[dict[str, str]] | None = None,
) -> str:
    """Render the persistent chrome page (title bar + sidebar + content iframe).

    is_mac controls whether macOS-specific styling is applied (traffic light padding,
    hidden window controls).

    In Electron mode, the iframe and browser sidebar are hidden via JS; the content
    and sidebar are handled by separate WebContentsViews.
    """
    template = _JINJA_ENV.from_string(_CHROME_TEMPLATE)
    return template.render(
        is_mac=is_mac,
        is_authenticated=is_authenticated,
        initial_workspaces=initial_workspaces or [],
    )


@pure
def render_sidebar_page() -> str:
    """Render the standalone sidebar page for the Electron sidebar WebContentsView.

    This page shows the workspace list and subscribes to SSE updates. In Electron,
    clicking a workspace sends an IPC message via the preload bridge to navigate
    the content WebContentsView.
    """
    template = _JINJA_ENV.from_string(_SIDEBAR_TEMPLATE)
    return template.render()
