from collections.abc import Sequence
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape

from imbue.changelings.primitives import OneTimeCode
from imbue.changelings.primitives import ServerName
from imbue.imbue_common.pure import pure
from imbue.mng.primitives import AgentId

_JINJA_ENV: Final[Environment] = Environment(autoescape=select_autoescape(default=True))

_LANDING_PAGE_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head>
  <title>Changelings</title>
  <style>
    * { margin: 0; padding: 0; box-sizing: border-box; }
    body { font-family: system-ui, -apple-system, sans-serif; padding: 40px; background: whitesmoke; }
    h1 { margin-bottom: 24px; color: rgb(26, 26, 46); }
    .agent-list { list-style: none; }
    .agent-list li { margin-bottom: 8px; }
    .agent-list a {
      display: inline-block; padding: 12px 20px;
      background: rgb(26, 26, 46); color: white; text-decoration: none;
      border-radius: 6px; font-size: 16px;
    }
    .agent-list a:hover { background: rgb(42, 42, 78); }
    .empty-state { color: gray; font-size: 16px; }
  </style>
</head>
<body>
  <h1>Your Changelings</h1>
  {% if agent_ids %}
  <ul class="agent-list">
    {% for agent_id in agent_ids %}
    <li><a href="/agents/{{ agent_id }}/">{{ agent_id }}</a></li>
    {% endfor %}
  </ul>
  {% else %}
  <p class="empty-state">
    No changelings are accessible. Use a login link to authenticate with a changeling.
  </p>
  {% endif %}
</body>
</html>"""

_LOGIN_REDIRECT_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html>
<head><title>Authenticating...</title></head>
<body>
<p>Authenticating...</p>
<script>
window.location.href = '/authenticate?agent_id={{ agent_id }}&one_time_code={{ one_time_code }}';
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
    <p>Please generate a new login URL for this device. Each login URL can only be used once.</p>
  </div>
</body>
</html>"""


@pure
def render_landing_page(accessible_agent_ids: Sequence[AgentId]) -> str:
    """Render the landing page listing accessible changelings."""
    template = _JINJA_ENV.from_string(_LANDING_PAGE_TEMPLATE)
    return template.render(agent_ids=accessible_agent_ids)


@pure
def render_login_redirect_page(
    agent_id: AgentId,
    one_time_code: OneTimeCode,
) -> str:
    """Render the JS redirect page that forwards to /authenticate."""
    template = _JINJA_ENV.from_string(_LOGIN_REDIRECT_TEMPLATE)
    return template.render(agent_id=agent_id, one_time_code=one_time_code)


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
  <div class="back-link"><a href="/">Back to all changelings</a></div>
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
