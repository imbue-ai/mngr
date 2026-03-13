from collections.abc import Sequence
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.forwarding_server.agent_creator import AgentCreationInfo
from imbue.minds.primitives import OneTimeCode
from imbue.minds.primitives import ServerName
from imbue.mng.primitives import AgentId

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
  <title>Minds</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .agent-list { list-style: none; }
    .agent-list li { margin-bottom: 8px; }
    .agent-list a { """
    + """
      display: inline-block; padding: 12px 20px;
      background: rgb(26, 26, 46); color: white; text-decoration: none;
      border-radius: 6px; font-size: 16px;
    }
    .agent-list a:hover { background: rgb(42, 42, 78); }
    .empty-state { color: gray; font-size: 16px; }
    .create-section { margin-top: 32px; }
    .create-section a { color: rgb(26, 26, 46); text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Your Minds</h1>
  {% if agent_ids %}
  <ul class="agent-list">
    {% for agent_id in agent_ids %}
    <li><a href="/agents/{{ agent_id }}/">{{ agent_id }}</a></li>
    {% endfor %}
  </ul>
  <div class="create-section">
    <a href="/create">Create another mind</a>
  </div>
  {% else %}
  <p class="empty-state">
    No minds are accessible. Use a login link to authenticate with a mind.
  </p>
  {% endif %}
</body>
</html>"""
)

_CREATE_FORM_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Create a Mind</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .form-group { margin-bottom: 16px; }
    label { display: block; margin-bottom: 6px; font-size: 14px; color: rgb(60, 60, 80); }
    input[type="text"] {
      width: 100%; max-width: 500px; padding: 10px 14px;
      border: 1px solid rgb(200, 200, 210); border-radius: 6px; font-size: 16px;
    }
    input[type="text"]:focus { outline: none; border-color: rgb(26, 26, 46); }
    .help-text { margin-top: 4px; font-size: 13px; color: gray; }
    .back-link { margin-top: 24px; }
    .back-link a { color: rgb(26, 26, 46); text-decoration: underline; }
  </style>
</head>
<body>
  <h1>Create a Mind</h1>
  <form action="/create" method="post">
    <div class="form-group">
      <label for="git_url">Git repository URL</label>
      <input type="text" id="git_url" name="git_url" value="{{ git_url }}"
             placeholder="https://github.com/imbue-ai/simple_mind.git" required>
      <p class="help-text">The repository will be cloned and used as the agent's working directory.</p>
    </div>
    <button type="submit" class="btn">Create</button>
  </form>
  <div class="back-link"><a href="/">Back</a></div>
</body>
</html>"""
)

_CREATING_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Creating your mind...</title>
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
  </style>
</head>
<body>
  <h1>Creating your mind...</h1>
  <p class="status" id="status"><span class="spinner"></span> {{ status_text }}</p>
  <script>
    const agentId = '{{ agent_id }}';
    async function pollStatus() {
      try {
        const resp = await fetch('/api/create-agent/' + agentId + '/status');
        const data = await resp.json();
        const el = document.getElementById('status');
        if (data.status === 'DONE') {
          el.textContent = 'Done! Redirecting...';
          window.location.href = data.redirect_url;
          return;
        } else if (data.status === 'FAILED') {
          el.textContent = 'Failed: ' + data.error;
          el.classList.add('error');
          return;
        } else if (data.status === 'CREATING') {
          el.textContent = 'Creating agent...';
        }
      } catch (e) { /* keep polling */ }
      setTimeout(pollStatus, 2000);
    }
    pollStatus();
  </script>
</body>
</html>"""
)

_LOGIN_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Login - Minds</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .login-message { color: gray; font-size: 16px; }
  </style>
</head>
<body>
  <h1>Minds</h1>
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
) -> str:
    """Render the landing page listing accessible minds."""
    template = _JINJA_ENV.from_string(_LANDING_PAGE_TEMPLATE)
    return template.render(agent_ids=accessible_agent_ids)


_DEFAULT_GIT_URL: Final[str] = "https://github.com/imbue-ai/simple_mind.git"


@pure
def render_create_form(git_url: str = "") -> str:
    """Render the agent creation form page.

    When git_url is provided, the form field is pre-filled with that value.
    Defaults to the simple_mind repository URL when empty.
    """
    effective_url = git_url if git_url else _DEFAULT_GIT_URL
    template = _JINJA_ENV.from_string(_CREATE_FORM_TEMPLATE)
    return template.render(git_url=effective_url)


@pure
def render_creating_page(agent_id: AgentId, info: AgentCreationInfo) -> str:
    """Render the progress page shown while an agent is being created.

    The page polls /api/create-agent/{agent_id}/status and auto-redirects
    to the agent when creation completes.
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


_BACKEND_WAITING_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Starting up...</title>
  <style>
    """
    + _COMMON_STYLES
    + """
    .status { margin-top: 16px; font-size: 16px; color: rgb(60, 60, 80); }
    .spinner {
      display: inline-block; width: 20px; height: 20px;
      border: 3px solid rgb(200, 200, 210); border-top: 3px solid rgb(26, 26, 46);
      border-radius: 50%; animation: spin 1s linear infinite;
      vertical-align: middle; margin-right: 8px;
    }
    @keyframes spin { to { transform: rotate(360deg); } }
    .error { color: darkred; }
  </style>
</head>
<body>
  <h1>Starting up...</h1>
  <p class="status" id="status"><span class="spinner"></span> Waiting for agent to become available...</p>
  <script>
    const startTime = Date.now();
    const timeoutMs = {{ timeout_seconds }} * 1000;
    async function checkBackend() {
      if (Date.now() - startTime > timeoutMs) {
        document.getElementById('status').textContent = 'Agent is not available. It may still be starting up -- try refreshing the page.';
        document.getElementById('status').classList.add('error');
        return;
      }
      location.reload();
    }
    setTimeout(checkBackend, 3000);
  </script>
</body>
</html>"""
)


@pure
def render_backend_waiting_page(
    agent_id: AgentId,
    server_name: ServerName,
    timeout_seconds: int,
) -> str:
    """Render a waiting page shown when the backend is not yet available.

    The page auto-reloads every 3 seconds. If the backend becomes available,
    the normal proxy flow takes over. If the timeout expires, it shows an
    error message.
    """
    template = _JINJA_ENV.from_string(_BACKEND_WAITING_TEMPLATE)
    return template.render(
        agent_id=agent_id,
        server_name=server_name,
        timeout_seconds=timeout_seconds,
    )


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
    .server-list li { margin-bottom: 8px; }
    .server-list a {
      display: inline-block; padding: 12px 20px;
      background: rgb(26, 26, 46); color: white; text-decoration: none;
      border-radius: 6px; font-size: 16px;
    }
    .server-list a:hover { background: rgb(42, 42, 78); }
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
    <li><a href="/agents/{{ agent_id }}/{{ server_name }}/">{{ server_name }}</a></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty-state">
    No servers are currently running for this agent.
  </p>
  {% endif %}
  <div class="back-link"><a href="/">Back to all minds</a></div>
</body>
</html>"""


@pure
def render_agent_servers_page(
    agent_id: AgentId,
    server_names: Sequence[ServerName],
) -> str:
    """Render a page listing all available servers for a specific agent."""
    template = _JINJA_ENV.from_string(_AGENT_SERVERS_TEMPLATE)
    return template.render(agent_id=agent_id, server_names=server_names)
