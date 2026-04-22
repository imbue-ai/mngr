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
  <title>Projects</title>
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
    tbody td:last-child { width: 48px; }
    tbody td:last-child .menu-btn { float: right; }
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
      <p class="empty-state" style="margin-bottom: 24px;">No projects yet</p>
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
  <title>Create a Project</title>
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
      <a href="/">Back to project list</a>
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
          <span id="lima-status" style="margin-left:8px;font-size:12px;color:#94a3b8;"></span>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="include_env_file">Include .env file</label>
          <p class="help-text">Ships a local ".env" to the agent host. Ignored for git URLs.</p>
        </div>
        <div class="form-input">
          <input type="checkbox" id="include_env_file" name="include_env_file" value="1" checked>
        </div>
      </div>
    </form>
  </div>
  <script>
    // When LIMA mode is selected, kick off a background download of the
    // bundled limactl (a no-op if it is already cached or a system
    // install is on PATH). The submit handler awaits this promise so
    // users can click Create before the download finishes.
    var limaInstallPromise = null;

    function startLimaInstallIfNeeded() {
      if (typeof window.minds === "undefined" || !window.minds.ensureLima) {
        return;
      }
      if (limaInstallPromise) return;
      var status = document.getElementById("lima-status");
      if (status) status.textContent = "Checking lima...";
      limaInstallPromise = window.minds.ensureLima().then(function (result) {
        if (!result || !result.ok) {
          if (status) {
            status.textContent = "Lima install failed: " +
              (result && result.error ? result.error : "unknown error");
            status.style.color = "#b91c1c";
          }
          throw new Error(result && result.error || "lima install failed");
        }
        if (status) status.textContent = "";
      });
      limaInstallPromise.catch(function () {
        // Reset so a retry (mode change) can try again.
        limaInstallPromise = null;
      });
    }

    if (typeof window.minds !== "undefined" && window.minds.onLimaProgress) {
      window.minds.onLimaProgress(function (pct) {
        var status = document.getElementById("lima-status");
        if (status) status.textContent = "Installing lima... " + pct + "%";
      });
    }

    var launchModeSelect = document.getElementById("launch_mode");
    if (launchModeSelect) {
      launchModeSelect.addEventListener("change", function () {
        if (launchModeSelect.value === "LIMA") startLimaInstallIfNeeded();
      });
      if (launchModeSelect.value === "LIMA") startLimaInstallIfNeeded();
    }

    var createForm = document.getElementById("create-form");
    if (createForm) {
      createForm.addEventListener("submit", function (e) {
        if (launchModeSelect && launchModeSelect.value === "LIMA" &&
            limaInstallPromise) {
          e.preventDefault();
          limaInstallPromise.then(function () {
            createForm.submit();
          }).catch(function () {
            // error already shown in status pill
          });
        }
      });
    }
  </script>
</body>
</html>"""
)

_CREATING_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Creating your project...</title>
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
  <h1>Creating your project...</h1>
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
  <title>Login - Projects</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .login-message { color: gray; font-size: 16px; }
  </style>
</head>
<body>
  <h1>Projects</h1>
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
    launch_mode: LaunchMode = LaunchMode.LIMA,
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
  <p class="empty-state" id="empty-state">
    Starting up &mdash; waiting for services to register&hellip;
  </p>
  <p class="empty-state" id="empty-state-final" style="display: none;">
    No servers are currently running for this agent.
  </p>
  {% endif %}
  <div class="back-link"><a href="/">Back to all projects</a></div>
  <script>
  // When the servers list is empty, the resolver hasn't received the
  // `server_registered` events from the VM's app-watcher yet. This is
  // common right after agent creation completes: the DONE status arrives
  // before the in-VM forward_port.py registrations propagate back. Retry
  // the default-redirect URL every ~2s so that the first retry after
  // events arrive promotes us to the correct server.
  (function() {
    var emptyState = document.getElementById('empty-state');
    if (!emptyState) return;
    var AGENT_ID = {{ agent_id|tojson }};
    var RETRY_KEY = 'servers-retry:' + AGENT_ID;
    var MAX_RETRIES = 30; // ~60 seconds at 2s/retry
    var n = parseInt(sessionStorage.getItem(RETRY_KEY) || '0', 10);
    if (n >= MAX_RETRIES) {
      // Give up and show the hard "nothing running" message so the
      // user can click Back. Clearing the counter so manual reload
      // starts fresh.
      sessionStorage.removeItem(RETRY_KEY);
      emptyState.style.display = 'none';
      var finalMsg = document.getElementById('empty-state-final');
      if (finalMsg) finalMsg.style.display = '';
      return;
    }
    sessionStorage.setItem(RETRY_KEY, String(n + 1));
    setTimeout(function() {
      // Go to the default-redirect URL (not this one). If servers are
      // now registered, it'll redirect to the preferred server
      // automatically; otherwise it'll bounce back here and retry.
      location.replace('/forwarding/' + AGENT_ID + '/');
    }, 2000);
  })();
  // A page render that DID find servers means the wait is over; clear the
  // retry counter so the next fresh open of this agent starts from zero.
  (function() {
    if (document.querySelector('.server-list')) {
      sessionStorage.removeItem('servers-retry:' + {{ agent_id|tojson }});
    }
  })();
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

_CHROME_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
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
  height: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
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

#update-btn {
  -webkit-app-region: no-drag;
  display: none;
  width: auto !important; height: auto !important;
  background: #3b82f6; color: #fff !important;
  cursor: pointer; padding: 4px 10px; border-radius: 4px;
  font-size: 12px; font-family: inherit; white-space: nowrap;
  margin-right: 6px; border: none;
}
#update-btn:hover { background: #2563eb; }

.minds-wc { display: flex; }
{% if is_mac %}.minds-wc { display: none; }{% endif %}
.minds-wc button { border-radius: 0; width: 36px; height: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px; }
.minds-wc button:hover { background: rgba(255,255,255,0.08); border-radius: 0; }
.minds-wc button:last-child:hover { background: rgb(220, 38, 38); color: white; border-radius: 0; }

/* Sidebar (browser mode) */
#sidebar-panel {
  position: fixed; left: 0; top: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
  width: 260px; height: calc(100% - """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px);
  background: #0f172a; z-index: 50;
  box-shadow: 4px 0 12px rgba(0,0,0,0.3);
  transform: translateX(-100%);
  transition: transform 200ms ease-in-out;
  overflow-y: auto;
  padding: 0;
}
#sidebar-panel.sidebar-visible { transform: translateX(0); }

.sidebar-item {
  padding: 10px 12px; cursor: pointer; font-size: 13px; font-weight: 500;
  color: #cbd5e1; border-radius: 6px; margin: 2px 0;
  transition: background 100ms;
}
.sidebar-item:hover { background: rgba(255,255,255,0.06); }

.sidebar-empty {
  padding: 24px 16px; font-size: 13px; color: #64748b; text-align: center;
}

/* Content area (browser mode) */
#content-frame {
  position: fixed; left: 0; top: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
  width: 100%; height: calc(100% - """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px);
  border: none;
}
</style>
</head>
<body>
<div id="minds-titlebar">
  <div class="minds-nav">
    <button id="sidebar-toggle" title="Projects">
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
  <button id="update-btn" title="A new version is ready. Click to restart and apply." style="display:none;">Update</button>
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
    <div class="sidebar-empty">No projects</div>
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

// -- Non-blocking auto-update button (Electron only) --
// The "Update" button stays hidden until ToDesktop reports that an update
// has finished downloading. Clicking it restarts the app and applies the
// update. Handles the race where the update finished before this page
// loaded by querying current state once on load.
if (isElectron) {
  var updateBtn = document.getElementById('update-btn');
  updateBtn.onclick = function() { window.minds.installUpdate(); };
  function showUpdateButton() { updateBtn.style.display = 'inline-block'; }
  window.minds.onUpdateReady(showUpdateButton);
  if (window.minds.isUpdateReady) {
    window.minds.isUpdateReady().then(function(ready) {
      if (ready) showUpdateButton();
    });
  }
}

// -- Title tracking + auth refresh on navigation --
function refreshAuthStatus() {
  fetch('/auth/api/status').then(function(r) { return r.json(); }).then(updateAuthUI).catch(function() {});
}

if (isElectron) {
  // In Electron, main process pushes an authoritative per-window title
  // (mirrors the OS window title: "{workspace-name} -- Minds" or "Minds").
  // Ignore content document.title entirely.
  if (window.minds.onWindowTitleChange) {
    window.minds.onWindowTitleChange(function(title) {
      document.getElementById('page-title').textContent = title || 'Minds';
    });
  } else {
    window.minds.onContentTitleChange(function(title) {
      document.getElementById('page-title').textContent = title || 'Minds';
    });
  }
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
    container.innerHTML = '<div class="sidebar-empty">No projects</div>';
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
    var label = key === 'Private' ? 'PRIVATE' : key;
    html += '<div style="padding:8px 12px 2px;font-size:11px;color:#64748b;letter-spacing:0.3px;">' + label + '</div>';
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

function handleChromeEvent(data) {
  try {
    if (data.type === 'workspaces') renderWorkspaces(data.workspaces);
    if (data.type === 'auth_status') updateAuthUI(data);
    if (data.type === 'request_count') updateRequestsBadge(data.count);
  } catch(e) {}
}

if (isElectron && window.minds.onChromeEvent) {
  // Electron: main process maintains a single SSE to /_chrome/events and
  // pushes events to every chrome/sidebar view. Each view opening its own
  // EventSource used to saturate Chromium's 6-connection-per-host cap.
  window.minds.onChromeEvent(handleChromeEvent);
} else {
  var evtSource = null;
  function connectSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/_chrome/events');
    evtSource.onmessage = function(event) {
      try { handleChromeEvent(JSON.parse(event.data)); } catch(e) {}
    };
    evtSource.onerror = function() {
      evtSource.close();
      evtSource = null;
      setTimeout(connectSSE, 5000);
    };
  }
  connectSSE();
}
</script>
</body>
</html>"""
)


_SIDEBAR_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Projects</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  background: #0f172a;
  overflow-y: auto;
  padding: 0;
}

h2 {
  font-size: 15px; color: #e2e8f0; padding: 12px;
  margin: 0; border-bottom: 1px solid #334155; font-weight: 500;
}

.sidebar-item {
  position: relative;
  padding: 10px 36px 10px 12px;
  cursor: pointer; font-size: 13px; font-weight: 500;
  color: #cbd5e1; border-radius: 6px; margin: 2px 0;
  transition: background 100ms;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.sidebar-item:hover { background: rgba(255,255,255,0.06); }

.sidebar-item-label {
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.sidebar-open-new {
  display: none;
  background: none; border: none; padding: 4px; cursor: pointer;
  color: #94a3b8; border-radius: 4px;
  align-items: center; justify-content: center;
}
.sidebar-open-new:hover { color: #e2e8f0; background: rgba(255,255,255,0.08); }
.sidebar-open-new svg { width: 14px; height: 14px; fill: none; stroke: currentColor;
  stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.sidebar-item:hover .sidebar-open-new { display: inline-flex; }
.sidebar-item.is-current .sidebar-open-new { display: none !important; }

.sidebar-empty {
  padding: 24px 16px; font-size: 13px; color: #64748b; text-align: center;
}
</style>
</head>
<body>
<h2>Projects</h2>
<div id="sidebar-workspaces">
  <div class="sidebar-empty">No projects</div>
</div>
<script>
var isElectron = !!window.minds;
var currentWorkspaceId = null;
var lastWorkspaces = [];

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, function(c) {
    return { '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[c];
  });
}

function selectWorkspace(agentId) {
  if (isElectron) window.minds.navigateContent('/forwarding/' + agentId + '/');
}

function openInNewWindow(agentId) {
  if (isElectron && window.minds.openWorkspaceInNewWindow) {
    window.minds.openWorkspaceInNewWindow(agentId);
  }
}

function renderWorkspaces(workspaces) {
  var container = document.getElementById('sidebar-workspaces');
  if (!workspaces || workspaces.length === 0) {
    container.innerHTML = '<div class="sidebar-empty">No projects</div>';
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
  var openIcon = '<svg viewBox="0 0 24 24"><path d="M14 3h7v7"/>'
    + '<path d="M10 14L21 3"/>'
    + '<path d="M21 14v5a2 2 0 01-2 2H5a2 2 0 01-2-2V5a2 2 0 012-2h5"/></svg>';
  var html = '';
  keys.forEach(function(key) {
    var label = key === 'Private' ? 'PRIVATE' : escapeHtml(key);
    html += '<div style="padding:8px 12px 2px;font-size:11px;color:#64748b;letter-spacing:0.3px;">' + label + '</div>';
    groups[key].forEach(function(w) {
      var id = escapeHtml(w.id);
      var name = escapeHtml(w.name || w.id);
      var isCurrent = w.id === currentWorkspaceId;
      var classes = 'sidebar-item' + (isCurrent ? ' is-current' : '');
      html += '<div class="' + classes + '" data-agent-id="' + id + '">'
        + '<span class="sidebar-item-label">' + name + '</span>'
        + '<button class="sidebar-open-new" data-open-new="' + id + '" title="Open in new window" tabindex="-1">'
        + openIcon
        + '</button>'
        + '</div>';
    });
  });
  container.innerHTML = html;
}

function handleRowClick(target) {
  var row = target.closest('.sidebar-item');
  if (!row) return;
  var openNewBtn = target.closest('.sidebar-open-new');
  var agentId = row.getAttribute('data-agent-id');
  if (!agentId) return;
  if (openNewBtn) {
    openInNewWindow(agentId);
    return;
  }
  selectWorkspace(agentId);
}

document.addEventListener('click', function(e) {
  handleRowClick(e.target);
});

document.addEventListener('contextmenu', function(e) {
  var row = e.target.closest('.sidebar-item');
  if (!row) return;
  var agentId = row.getAttribute('data-agent-id');
  if (!agentId) return;
  // Suppress context menu for the current workspace row -- nothing actionable there.
  if (agentId === currentWorkspaceId) {
    e.preventDefault();
    return;
  }
  e.preventDefault();
  if (isElectron && window.minds.showWorkspaceContextMenu) {
    window.minds.showWorkspaceContextMenu(agentId, e.clientX, e.clientY);
  }
});

if (isElectron && window.minds.onCurrentWorkspaceChanged) {
  window.minds.onCurrentWorkspaceChanged(function(agentId) {
    currentWorkspaceId = agentId || null;
    renderWorkspaces(lastWorkspaces);
  });
}

function handleChromeEvent(data) {
  if (data.type !== 'workspaces') return;
  lastWorkspaces = data.workspaces || [];
  renderWorkspaces(lastWorkspaces);
}

if (isElectron && window.minds.onChromeEvent) {
  // In Electron, the main process maintains the single SSE connection and
  // pushes events to us via IPC. See main.js/runChromeSSELoop.
  window.minds.onChromeEvent(handleChromeEvent);
} else {
  var evtSource = null;
  function connectSSE() {
    if (evtSource) evtSource.close();
    evtSource = new EventSource('/_chrome/events');
    evtSource.onmessage = function(event) {
      try { handleChromeEvent(JSON.parse(event.data)); } catch(e) {}
    };
    evtSource.onerror = function() {
      evtSource.close();
      evtSource = null;
      setTimeout(connectSSE, 5000);
    };
  }
  connectSSE();
}
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


# -- Page styles shared across settings, sharing, and accounts pages --

_PAGE_STYLES: Final[str] = """
    body { background: #f8fafc; padding: 0; font-size: 14px;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 0; }
    .page { max-width: 640px; margin: 0 auto; padding: 48px 24px; }
    h1 { font-size: 20px; color: #0f172a; margin-bottom: 4px; }
    h2 { font-size: 15px; color: #64748b; margin: 28px 0 10px; padding-top: 20px;
      border-top: 1px solid #e2e8f0; font-weight: 500; }
    p { color: #334155; margin: 6px 0; font-size: 14px; line-height: 1.5; }
    a { color: #2563eb; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .subtitle { color: #94a3b8; font-size: 12px; margin-bottom: 20px; }
    .btn { display: inline-block; padding: 8px 16px; border: none; border-radius: 6px;
      cursor: pointer; font-size: 13px; font-weight: 500; font-family: inherit; }
    .btn-primary { background: #1e293b; color: white; }
    .btn-primary:hover { background: #334155; }
    .btn-success { background: #065f46; color: #d1fae5; }
    .btn-success:hover { background: #047857; }
    .btn-danger { background: #fef2f2; color: #dc2626; border: 1px solid #fecaca; }
    .btn-danger:hover { background: #fee2e2; }
    .btn-secondary { background: #f1f5f9; color: #334155; border: 1px solid #e2e8f0; }
    .btn-secondary:hover { background: #e2e8f0; }
    .btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .select-input { padding: 8px 12px; border-radius: 6px; background: white;
      color: #0f172a; border: 1px solid #cbd5e1; font-size: 13px; font-family: inherit; }
    .card { background: white; border: 1px solid #e2e8f0; border-radius: 8px; padding: 16px; margin: 8px 0; }
    .warning { color: #92400e; font-size: 13px; background: #fffbeb; border: 1px solid #fde68a;
      border-radius: 6px; padding: 8px 12px; margin: 8px 0; }
    .input-row { display: flex; gap: 8px; margin: 8px 0; }
    .text-input { flex: 1; padding: 8px 12px; border-radius: 6px; border: 1px solid #cbd5e1;
      font-size: 13px; font-family: inherit; }
    .email-tag { display: inline-flex; align-items: center; gap: 4px; background: #f1f5f9;
      border: 1px solid #e2e8f0; border-radius: 4px; padding: 4px 8px; margin: 2px; font-size: 13px; }
    .email-tag button { background: none; border: none; cursor: pointer; color: #94a3b8;
      font-size: 16px; line-height: 1; padding: 0 2px; }
    .email-tag button:hover { color: #dc2626; }
    .url-box { display: flex; gap: 8px; align-items: center; background: #f8fafc;
      border: 1px solid #e2e8f0; border-radius: 6px; padding: 8px 12px; margin: 8px 0; }
    .url-box input { flex: 1; background: none; border: none; font-size: 13px; color: #0f172a;
      font-family: monospace; outline: none; }
    .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
    .status-enabled { background: #22c55e; }
    .status-disabled { background: #94a3b8; }
    .loading { color: #94a3b8; padding: 16px 0; }
    .error { color: #dc2626; margin: 8px 0; }
    .actions { display: flex; gap: 8px; margin-top: 20px; }
"""


_ASSOCIATE_SNIPPET: Final[str] = """
    <div class="card" style="margin:12px 0;">
      <p style="font-weight:500;margin-bottom:8px;">This workspace needs to be associated with an account before sharing can be configured.</p>
      {% if accounts %}
      <form method="POST" action="/workspace/{{ agent_id }}/associate" style="display:flex;gap:8px;align-items:center;margin-top:8px;">
        <select name="user_id" class="select-input">
          {% for acct in accounts %}
          <option value="{{ acct.user_id }}">{{ acct.email }}</option>
          {% endfor %}
        </select>
        {% if redirect_url %}<input type="hidden" name="redirect" value="{{ redirect_url }}">{% endif %}
        <button type="submit" class="btn btn-primary">Associate</button>
      </form>
      {% else %}
      <p style="margin-top:8px;"><a href="/auth/login">Sign in or create an account</a> to enable sharing.</p>
      {% endif %}
    </div>
"""


_SHARING_EDITOR_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>{{ title }}</title>
  <style>"""
    + _PAGE_STYLES
    + """
    .acl-row { display:flex; align-items:center; justify-content:space-between;
      padding:8px 12px; border:1px solid #e2e8f0; border-radius:6px; margin:4px 0; }
    .acl-existing { background:white; }
    .acl-added { background:#f0fdf4; border-color:#bbf7d0; }
    .acl-removed { background:#fef2f2; border-color:#fecaca; text-decoration:line-through; }
    .acl-prefix { font-weight:600; margin-right:6px; font-size:14px; }
    .acl-prefix-add { color:#16a34a; }
    .acl-prefix-remove { color:#dc2626; }
    .acl-x { background:none; border:none; cursor:pointer; color:#94a3b8;
      font-size:18px; line-height:1; padding:0 4px; }
    .acl-x:hover { color:#64748b; }
  </style>
</head>
<body>
  <div class="page">
    <h1 id="page-heading">Share <code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:18px;">{{ server_name }}</code>
      in <a href="/forwarding/{{ agent_id }}/" style="font-size:20px;">{{ ws_name or agent_id }}</a>
      {% if account_email %}(<a href="/accounts">{{ account_email }}</a>){% endif %}?</h1>

    {% if not has_account %}
    """
    + _ASSOCIATE_SNIPPET
    + """
    {% if is_request %}
    <form method="POST" action="/requests/{{ request_id }}/deny" style="margin-top:8px;">
      <button type="submit" class="btn btn-danger">Deny request</button>
    </form>
    {% endif %}
    {% else %}

    <div id="sharing-editor">
      <div class="loading" id="loading-state">Loading...</div>
    </div>

    <div id="editor-content" style="display:none;">
      <div id="url-section" style="display:none;margin-bottom:16px;">
        <p style="font-weight:500;margin-bottom:4px;">Shared URL</p>
        <div class="url-box">
          <input type="text" id="share-url" readonly onclick="this.select()">
          <button class="btn btn-secondary" onclick="copyUrl()" id="copy-btn">Copy</button>
        </div>
      </div>

      <h2 style="border-top:none;padding-top:0;margin-top:0;">Access List</h2>
      <div id="email-list"></div>
      <div class="input-row">
        <input type="email" class="text-input" id="new-email" placeholder="Add email address"
          onkeydown="if(event.key==='Enter'){event.preventDefault();addEmail();}">
        <button class="btn btn-secondary" onclick="addEmail()">Add</button>
      </div>

      <div class="actions" id="action-buttons" style="justify-content:space-between;">
        {% if is_request %}
        <button class="btn btn-danger" id="deny-btn" onclick="submitDeny()">Deny</button>
        {% else %}
        <div style="display:flex;gap:8px;">
          <button class="btn btn-secondary" onclick="window.location='/workspace/{{ agent_id }}/settings'">Cancel</button>
          <button class="btn btn-danger" id="disable-btn" onclick="submitDisable()" style="display:none;">
            Disable Sharing
          </button>
        </div>
        {% endif %}
        <button class="btn btn-success" id="action-btn" onclick="submitUpdate()">
          Update
        </button>
      </div>
      <div id="submit-spinner" style="display:none;padding:16px 0;">
        <span style="color:#94a3b8;">Saving changes...</span>
      </div>
    </div>
  </div>

  <script>
  var proposedEmails = {{ initial_emails | tojson }};
  var serverName = {{ server_name | tojson }};
  var agentId = {{ agent_id | tojson }};
  var isRequest = {{ is_request | tojson }};
  var requestId = {{ request_id | tojson }};
  var wsName = {{ (ws_name or agent_id) | tojson }};
  var accountEmail = {{ (account_email or '') | tojson }};

  function setHeading(isEnabled) {
    var h = document.getElementById('page-heading');
    if (!h) return;
    var code = '<code style="background:#f1f5f9;padding:2px 6px;border-radius:4px;font-size:18px;">' + serverName + '</code>';
    var ws = '<a href="/forwarding/' + agentId + '/" style="font-size:20px;">' + wsName + '</a>';
    var acct = accountEmail ? ' (<a href="/accounts">' + accountEmail + '</a>)' : '';
    if (isEnabled) {
      h.innerHTML = code + ' shared in ' + ws + acct;
    } else {
      h.innerHTML = 'Share ' + code + ' in ' + ws + acct + '?';
    }
  }

  // Three-state ACL: existing (already on server), added (proposed new), removed (proposed removal)
  var existing = [];  // emails currently on the server
  var added = [];     // emails to add
  var removed = [];   // emails to remove from existing

  function renderACL() {
    var container = document.getElementById('email-list');
    var rows = [];

    // Existing emails (not removed)
    existing.forEach(function(e) {
      if (removed.indexOf(e) >= 0) return;
      rows.push(
        '<div class="acl-row acl-existing">' +
        '<span style="font-size:13px;color:#334155;">' + e + '</span>' +
        '<button class="acl-x" onclick="markRemoved(\\'' + e + '\\')">&times;</button></div>'
      );
    });

    // Added emails
    added.forEach(function(e) {
      rows.push(
        '<div class="acl-row acl-added">' +
        '<span><span class="acl-prefix acl-prefix-add">+</span>' +
        '<span style="font-size:13px;color:#334155;">' + e + '</span></span>' +
        '<button class="acl-x" onclick="unmarkAdded(\\'' + e + '\\')">&times;</button></div>'
      );
    });

    // Removed emails
    removed.forEach(function(e) {
      rows.push(
        '<div class="acl-row acl-removed">' +
        '<span><span class="acl-prefix acl-prefix-remove">&minus;</span>' +
        '<span style="font-size:13px;color:#94a3b8;">' + e + '</span></span>' +
        '<button class="acl-x" onclick="unmarkRemoved(\\'' + e + '\\')">&times;</button></div>'
      );
    });

    if (rows.length === 0) {
      container.innerHTML = '<p style="color:#94a3b8;font-size:13px;">No one in the access list</p>';
    } else {
      container.innerHTML = rows.join('');
    }
  }

  function addEmail() {
    var input = document.getElementById('new-email');
    var email = input.value.trim();
    if (!email) return;
    // If it's in removed, just un-remove it (restore to existing)
    if (removed.indexOf(email) >= 0) {
      removed = removed.filter(function(e) { return e !== email; });
    } else if (existing.indexOf(email) < 0 && added.indexOf(email) < 0) {
      added.push(email);
    }
    input.value = '';
    renderACL();
  }

  function markRemoved(email) {
    if (removed.indexOf(email) < 0) removed.push(email);
    renderACL();
  }

  function unmarkAdded(email) {
    added = added.filter(function(e) { return e !== email; });
    renderACL();
  }

  function unmarkRemoved(email) {
    removed = removed.filter(function(e) { return e !== email; });
    renderACL();
  }

  function getFinalEmails() {
    var result = existing.filter(function(e) { return removed.indexOf(e) < 0; });
    return result.concat(added);
  }

  function setSubmitting(submitting) {
    document.getElementById('action-buttons').style.display = submitting ? 'none' : 'flex';
    document.getElementById('submit-spinner').style.display = submitting ? 'block' : 'none';
    var inputs = document.querySelectorAll('input, button, select');
    inputs.forEach(function(el) { el.disabled = submitting; });
    var editor = document.getElementById('editor-content');
    editor.style.opacity = submitting ? '0.5' : '1';
    editor.style.pointerEvents = submitting ? 'none' : 'auto';
  }

  function submitUpdate() {
    setSubmitting(true);
    var form = new FormData();
    form.append('emails', JSON.stringify(getFinalEmails()));
    fetch('/sharing/' + agentId + '/' + serverName + '/enable', { method: 'POST', body: form })
      .then(function(r) { window.location.href = '/sharing/' + agentId + '/' + serverName; })
      .catch(function(err) { alert('Failed: ' + err.message); setSubmitting(false); });
  }

  function submitDisable() {
    setSubmitting(true);
    fetch('/sharing/' + agentId + '/' + serverName + '/disable', { method: 'POST' })
      .then(function(r) { window.location.href = '/sharing/' + agentId + '/' + serverName; })
      .catch(function(err) { alert('Failed: ' + err.message); setSubmitting(false); });
  }

  function submitDeny() {
    setSubmitting(true);
    fetch('/requests/' + requestId + '/deny', { method: 'POST' })
      .then(function(r) { window.location.href = '/'; })
      .catch(function(err) { alert('Failed: ' + err.message); setSubmitting(false); });
  }

  function copyUrl() {
    var input = document.getElementById('share-url');
    navigator.clipboard.writeText(input.value);
    var btn = document.getElementById('copy-btn');
    btn.textContent = 'Copied';
    setTimeout(function() { btn.textContent = 'Copy'; }, 2000);
  }

  // Load current sharing status, then compute the diff
  fetch('/api/sharing-status/' + agentId + '/' + serverName)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      document.getElementById('loading-state').style.display = 'none';
      document.getElementById('editor-content').style.display = 'block';

      // Extract emails from auth_rules
      var serverEmails = [];
      if (data.auth_rules) {
        data.auth_rules.forEach(function(rule) {
          (rule.include || []).forEach(function(inc) {
            if (inc.email && inc.email.email && serverEmails.indexOf(inc.email.email) < 0) {
              serverEmails.push(inc.email.email);
            }
          });
        });
      }

      if (data.enabled) {
        // Sharing is already on: server emails are "existing"
        existing = serverEmails;
        document.getElementById('action-btn').textContent = 'Update';
        setHeading(true);
        if (data.url) {
          document.getElementById('url-section').style.display = 'block';
          document.getElementById('share-url').value = data.url;
        }
        var disableBtn = document.getElementById('disable-btn');
        if (disableBtn) disableBtn.style.display = 'inline-block';
      } else {
        // Not yet enabled: default tunnel permissions + proposed are all "added"
        serverEmails.forEach(function(e) {
          if (added.indexOf(e) < 0) added.push(e);
        });
        document.getElementById('action-btn').textContent = 'Share';
        setHeading(false);
      }

      // Proposed emails that aren't already existing or added go to added
      proposedEmails.forEach(function(e) {
        if (existing.indexOf(e) < 0 && added.indexOf(e) < 0) {
          added.push(e);
        }
      });

      renderACL();
    })
    .catch(function(err) {
      document.getElementById('loading-state').innerHTML =
        '<p class="error">Failed to load sharing status: ' + err.message + '</p>';
      document.getElementById('editor-content').style.display = 'block';
      // Fall back: treat all proposed as added
      added = proposedEmails.slice();
      renderACL();
    });
  </script>
    {% endif %}
</body>
</html>"""
)


@pure
def render_sharing_editor(
    agent_id: str,
    server_name: str,
    title: str,
    initial_emails: list[str] | None = None,
    is_request: bool = False,
    request_id: str = "",
    has_account: bool = True,
    accounts: Sequence[object] | None = None,
    redirect_url: str = "",
    ws_name: str = "",
    account_email: str = "",
) -> str:
    """Render the sharing editor page used for both request approval and direct editing."""
    template = _JINJA_ENV.from_string(_SHARING_EDITOR_TEMPLATE)
    return template.render(
        title=title,
        agent_id=agent_id,
        server_name=server_name,
        initial_emails=initial_emails or [],
        is_request=is_request,
        request_id=request_id,
        has_account=has_account,
        accounts=accounts or [],
        redirect_url=redirect_url,
        ws_name=ws_name,
        account_email=account_email,
    )


_WORKSPACE_SETTINGS_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Settings: {{ ws_name }}</title>
  <style>"""
    + _PAGE_STYLES
    + """
  </style>
</head>
<body>
  <div class="page">
    <h1>{{ ws_name }}</h1>
    <p class="subtitle">{{ agent_id }}</p>

    <h2>Account</h2>
    <div id="account-section">
    {% if current_account %}
    <p>Associated with: <strong>{{ current_account.email }}</strong></p>
    <p class="warning">Disassociating will remove all sharing (tunnels) for this workspace.
      You will need to set up sharing again after re-associating.</p>
    <button class="btn btn-danger" id="disassociate-btn" onclick="submitDisassociate()">Disassociate</button>
    <span id="disassociate-spinner" style="display:none;color:#94a3b8;margin-left:8px;">Disassociating...</span>
    {% else %}
    """
    + _ASSOCIATE_SNIPPET
    + """
    {% endif %}
    </div>

    <h2>Sharing</h2>
    {% for server in servers %}
    <div class="card" style="display:flex;justify-content:space-between;align-items:center;">
      <span style="font-weight:500;">{{ server }}</span>
      <a href="/sharing/{{ agent_id }}/{{ server }}" class="btn btn-secondary">Manage sharing</a>
    </div>
    {% else %}
    <p style="color:#94a3b8;">No servers discovered for this workspace.</p>
    {% endfor %}

    {% if telegram_section %}
    <h2>Telegram</h2>
    {{ telegram_section | safe }}
    {% endif %}

    <h2>Danger Zone</h2>
    <p style="color:#94a3b8;font-size:13px;margin-bottom:8px;">
      Permanently delete this workspace and all its data.</p>
    <button class="btn btn-danger" id="delete-workspace-btn" onclick="submitDeleteWorkspace()">Delete workspace</button>
    <span id="delete-workspace-spinner" style="display:none;margin-left:8px;color:#94a3b8;">Deleting...</span>

    <div style="margin-top:24px;"><a href="/">&larr; Back to workspaces</a></div>
  </div>

  <script>
  function submitDisassociate() {
    var btn = document.getElementById('disassociate-btn');
    var spinner = document.getElementById('disassociate-spinner');
    btn.disabled = true;
    spinner.style.display = 'inline';
    var section = document.getElementById('account-section');
    section.style.opacity = '0.5';
    section.style.pointerEvents = 'none';
    fetch('/workspace/{{ agent_id }}/disassociate', { method: 'POST' })
      .then(function() { window.location.reload(); })
      .catch(function(err) {
        alert('Failed: ' + err.message);
        btn.disabled = false;
        spinner.style.display = 'none';
        section.style.opacity = '1';
        section.style.pointerEvents = 'auto';
      });
  }

  function submitDeleteWorkspace() {
    // mngr destroy is not reversible: the Lima VM, host record, agent
    // state, and mngr/<name> git branch all go away. Confirm hard.
    if (!confirm("Permanently delete this workspace?\\n\\n" +
                 "This destroys the VM, removes the agent, and deletes the " +
                 "mngr/<name> branch. It cannot be undone.")) {
      return;
    }
    var btn = document.getElementById('delete-workspace-btn');
    var spinner = document.getElementById('delete-workspace-spinner');
    btn.disabled = true;
    spinner.style.display = 'inline';
    fetch('/workspace/{{ agent_id }}/delete', { method: 'POST' })
      .then(function(resp) {
        if (!resp.ok) {
          return resp.json().then(function(data) {
            throw new Error(data.error || ('HTTP ' + resp.status));
          });
        }
        return resp.json();
      })
      .then(function(data) {
        window.location.href = data.redirect_url || '/';
      })
      .catch(function(err) {
        alert('Failed to delete workspace: ' + err.message);
        btn.disabled = false;
        spinner.style.display = 'none';
      });
  }
  {% if telegram_js %}
  {{ telegram_js | safe }}
  {% endif %}
  </script>
</body>
</html>"""
)


@pure
def render_workspace_settings(
    agent_id: str,
    ws_name: str,
    current_account: object | None,
    accounts: Sequence[object],
    servers: Sequence[str],
    telegram_section: str = "",
    telegram_js: str = "",
) -> str:
    """Render the workspace settings page."""
    template = _JINJA_ENV.from_string(_WORKSPACE_SETTINGS_TEMPLATE)
    return template.render(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_section=telegram_section,
        telegram_js=telegram_js,
    )


_ACCOUNTS_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Manage Accounts</title>
  <style>"""
    + _PAGE_STYLES
    + """
  </style>
</head>
<body>
  <div class="page">
    <h1>Manage Accounts</h1>

    {% if accounts %}
    {% for acct in accounts %}
    <div class="card" style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <div style="font-weight:500;color:#0f172a;">{{ acct.email }}</div>
        <div style="font-size:12px;color:#94a3b8;">{{ acct.workspace_ids | length }} workspace(s)
          {% if acct.user_id | string == default_account_id %} &middot; Default{% endif %}</div>
      </div>
      <div style="display:flex;gap:8px;">
        {% if acct.user_id | string != default_account_id %}
        <form method="POST" action="/accounts/set-default">
          <input type="hidden" name="user_id" value="{{ acct.user_id }}">
          <button type="submit" class="btn btn-secondary">Set default</button>
        </form>
        {% else %}
        <span class="btn btn-secondary" style="cursor:default;opacity:0.6;">Default</span>
        {% endif %}
        <form method="POST" action="/accounts/{{ acct.user_id }}/logout">
          <button type="submit" class="btn btn-danger">Log out</button>
        </form>
      </div>
    </div>
    {% endfor %}
    {% else %}
    <p style="color:#94a3b8;">No accounts logged in.</p>
    {% endif %}

    <div style="margin-top:16px;">
      <a href="/auth/login" class="btn btn-primary">Add account</a>
    </div>
    <div style="margin-top:16px;"><a href="/">&larr; Back to workspaces</a></div>
  </div>
</body>
</html>"""
)


@pure
def render_accounts_page(
    accounts: Sequence[object],
    default_account_id: str | None = None,
) -> str:
    """Render the manage accounts page."""
    template = _JINJA_ENV.from_string(_ACCOUNTS_PAGE_TEMPLATE)
    return template.render(
        accounts=accounts,
        default_account_id=default_account_id or "",
    )
