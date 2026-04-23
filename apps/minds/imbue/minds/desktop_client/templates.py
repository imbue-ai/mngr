import hashlib
import os
from collections.abc import Sequence
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

_JINJA_ENV: Final[Environment] = Environment(autoescape=select_autoescape(default=True))


# -- Per-workspace identity color --
# Each workspace gets a deterministic color derived from a SHA-256 hash of
# its agent id, mapped into OKLCH with fixed lightness and chroma. OKLCH is
# perceptually uniform: a 15 degree hue shift looks like the same amount of
# change at any point on the wheel, which HSL doesn't give (HSL yellows all
# bunch up, HSL greens sprawl). Fixed L/C keeps every workspace at the same
# readable mid-tone and the same saturation, so the only axis of variation
# is the hue itself.
#
# The hue is a 32-bit hash mod 360, so collisions only happen if two ids
# hash to the exact same degree -- effectively never. Callers on the JS
# side (chrome, sidebar) mirror this function so the client picks the same
# color the server would have picked.

_WORKSPACE_L: Final[int] = 65  # percent
_WORKSPACE_C: Final[float] = 0.15


@pure
def workspace_accent(agent_id: str) -> str:
    """Deterministically map an agent id to a CSS OKLCH color.

    Uses a fixed lightness and chroma so every workspace accent sits at the
    same readable mid-tone, and only the hue varies. Full 360 degree hue
    range means collisions are effectively impossible, and OKLCH's
    perceptual uniformity means close hashes still read as visibly
    different colors.
    """
    digest = hashlib.sha256(agent_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") % 360
    return f"oklch({_WORKSPACE_L}% {_WORKSPACE_C} {hue})"


# -- Shared design tokens --
# Every template includes this block. Colors, spacing, type scale, radii,
# plus the primitive component classes (.btn, .card, .input, .spinner,
# .page, .notice). Page-specific CSS is only for layout unique to that page.
TOKENS: Final[str] = """
:root {
  --bg-page: #f7f7f8;
  --bg-surface: #ffffff;
  --bg-muted: #f4f4f5;
  --bg-chrome: #18181b;
  --bg-chrome-elev: #27272a;
  --bg-chrome-hover: rgba(255,255,255,0.06);
  --bg-chrome-active: rgba(255,255,255,0.10);

  --border: #e4e4e7;
  --border-strong: #d4d4d8;
  --border-chrome: rgba(255,255,255,0.08);

  --text: #18181b;
  --text-muted: #52525b;
  --text-subtle: #a1a1aa;
  --text-invert: #fafafa;
  --text-chrome: #e4e4e7;
  --text-chrome-muted: #a1a1aa;

  /* Workspace accent color. Set per element -- body for in-workspace pages,
     per row for landing/sidebar -- and used via var(--workspace-accent)
     directly. Do NOT introduce a separate --accent that wraps this via a
     nested var(): nested var() inside a custom property on :root resolves
     against :root, not against the element where the outer var() is used,
     so every workspace would pick up the same fallback. */
  --link: #2563eb;

  --danger: #dc2626;
  --danger-bg: #fef2f2;
  --danger-border: #fecaca;
  --success: #15803d;
  --success-bg: #f0fdf4;
  --success-border: #bbf7d0;
  --warning-text: #92400e;
  --warning-bg: #fffbeb;
  --warning-border: #fde68a;

  --radius-sm: 4px;
  --radius: 6px;
  --radius-lg: 10px;
  --radius-card: 12px;

  --shadow-card: 0 1px 2px rgba(0,0,0,0.04), 0 1px 3px rgba(0,0,0,0.05);
  --shadow-seam: 0 4px 10px -6px rgba(0,0,0,0.35);
  --focus-ring: 0 0 0 3px rgba(37,99,235,0.18);

  --font-sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Helvetica, Arial, sans-serif;
  --font-mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;

  --fs-xs: 12px;
  --fs-sm: 13px;
  --fs-md: 14px;
  --fs-lg: 16px;
  --fs-xl: 20px;
  --fs-2xl: 24px;
}

* { margin: 0; padding: 0; box-sizing: border-box; }

html, body { height: 100%; }
body {
  font-family: var(--font-sans);
  font-size: var(--fs-md);
  color: var(--text);
  background: var(--bg-page);
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

a { color: var(--link); text-decoration: none; }
a:hover { text-decoration: underline; }

.page { max-width: 720px; margin: 0 auto; padding: 48px 24px; }
.page-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; margin-bottom: 24px;
}
.page-header__back {
  color: var(--text-muted); font-size: var(--fs-sm);
}
.page-header__back:hover { color: var(--text); text-decoration: none; }

/* Workspace identity stripe -- 3px band in the project's hue at the very top. */
.page-workspace { position: relative; }
.page-workspace::before {
  content: "";
  position: fixed; top: 0; left: 0; right: 0; height: 3px;
  background: var(--workspace-accent, oklch(65% 0.15 230));
  z-index: 1000;
}

h1 { font-size: var(--fs-xl); font-weight: 600; color: var(--text); line-height: 1.3; }
h1.display { font-size: var(--fs-2xl); }
h2 {
  font-size: var(--fs-md); font-weight: 500; color: var(--text-muted);
  margin-top: 32px; margin-bottom: 12px; padding-top: 20px;
  border-top: 1px solid var(--border);
}
h2.tight { margin-top: 0; padding-top: 0; border-top: none; }
p { color: var(--text); line-height: 1.5; }
.subtitle { color: var(--text-subtle); font-size: var(--fs-xs); margin-bottom: 20px; }
.muted { color: var(--text-muted); }
.subtle { color: var(--text-subtle); }

code {
  background: var(--bg-muted); padding: 2px 6px; border-radius: var(--radius-sm);
  font-family: var(--font-mono); font-size: 0.95em;
}

.card {
  background: var(--bg-surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-lg);
  padding: 16px;
  box-shadow: var(--shadow-card);
}
.card + .card { margin-top: 8px; }
.card-row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }

.btn {
  display: inline-flex; align-items: center; justify-content: center; gap: 6px;
  padding: 8px 14px;
  border: 1px solid transparent;
  border-radius: var(--radius);
  font-family: inherit; font-size: var(--fs-sm); font-weight: 500;
  cursor: pointer; text-decoration: none;
  background: transparent; color: var(--text);
  transition: background 100ms, border-color 100ms, color 100ms;
  line-height: 1.2;
}
.btn:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-primary { background: var(--text); color: var(--text-invert); }
.btn-primary:hover { background: var(--bg-chrome-elev); text-decoration: none; color: var(--text-invert); }
.btn-secondary { background: var(--bg-muted); border-color: var(--border); color: var(--text); }
.btn-secondary:hover { background: var(--border); text-decoration: none; color: var(--text); }
.btn-danger { background: var(--danger-bg); color: var(--danger); border-color: var(--danger-border); }
.btn-danger:hover { background: #fee2e2; text-decoration: none; color: var(--danger); }
.btn-success { background: #166534; color: #ecfdf5; }
.btn-success:hover { background: #14532d; text-decoration: none; color: #ecfdf5; }
.btn-ghost { color: var(--text-muted); }
.btn-ghost:hover { background: var(--bg-muted); color: var(--text); text-decoration: none; }
.btn-sm { padding: 4px 10px; font-size: var(--fs-xs); }
.btn-block { width: 100%; }

.input, select.input {
  width: 100%;
  padding: 10px 12px;
  border: 1px solid var(--border);
  border-radius: var(--radius);
  background: var(--bg-surface);
  color: var(--text);
  font-family: inherit; font-size: var(--fs-sm);
  outline: none;
  transition: border-color 100ms, box-shadow 100ms;
}
.input:focus, select.input:focus { border-color: var(--link); box-shadow: var(--focus-ring); }

.input-row { display: flex; gap: 8px; align-items: center; }
.input-row .input { flex: 1; }

.form-group { display: flex; gap: 24px; margin-bottom: 16px; align-items: flex-start; }
.form-label { flex: 0 0 200px; padding-top: 10px; }
.form-label label { font-size: var(--fs-sm); color: var(--text); font-weight: 500; display: block; }
.form-help { margin-top: 2px; font-size: var(--fs-xs); color: var(--text-subtle); }
.form-input { flex: 1; }

.spinner {
  display: inline-block; vertical-align: middle;
  width: 18px; height: 18px; border-radius: 50%;
  border: 2px solid var(--border);
  border-top-color: var(--text);
  animation: spin 800ms linear infinite;
}
.spinner-lg { width: 32px; height: 32px; border-width: 3px; }
@keyframes spin { to { transform: rotate(360deg); } }

.notice { padding: 10px 12px; border-radius: var(--radius); font-size: var(--fs-sm); margin: 8px 0; }
.notice-info { background: #eff6ff; color: #1e40af; border: 1px solid #dbeafe; }
.notice-warn { background: var(--warning-bg); color: var(--warning-text); border: 1px solid var(--warning-border); }
.notice-success { background: var(--success-bg); color: var(--success); border: 1px solid var(--success-border); }
.notice-error { background: var(--danger-bg); color: var(--danger); border: 1px solid var(--danger-border); }

.empty-state { color: var(--text-subtle); font-size: var(--fs-md); text-align: center; padding: 48px 0; }

.url-box {
  display: flex; gap: 8px; align-items: center;
  background: var(--bg-muted); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 8px 12px; margin: 8px 0;
}
.url-box input {
  flex: 1; background: transparent; border: none; font-size: var(--fs-sm);
  color: var(--text); font-family: var(--font-mono); outline: none;
}

.status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
.status-enabled { background: var(--success); }
.status-disabled { background: var(--text-subtle); }

.actions { display: flex; gap: 8px; margin-top: 20px; }

/* Accent swatch -- tiny colored square tied to the workspace hue. */
.accent-swatch {
  display: inline-block;
  width: 10px; height: 10px;
  border-radius: 2px;
  background: var(--workspace-accent, oklch(65% 0.15 230));
  vertical-align: middle;
  flex-shrink: 0;
}
"""


_LANDING_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Projects</title>
  <style>"""
    + TOKENS
    + """
    .landing-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 20px; }
    .project-list { display: flex; flex-direction: column; gap: 6px; }
    .project-row {
      display: flex; align-items: center; gap: 12px;
      background: var(--bg-surface); border: 1px solid var(--border); border-radius: var(--radius-lg);
      padding: 14px 16px; cursor: pointer;
      transition: border-color 100ms, box-shadow 100ms;
      position: relative; overflow: hidden;
    }
    .project-row:hover { border-color: var(--border-strong); box-shadow: var(--shadow-card); }
    .project-row::before {
      content: ""; position: absolute; left: 0; top: 0; bottom: 0; width: 3px;
      background: var(--workspace-accent, oklch(65% 0.15 230));
    }
    .project-row__name { flex: 1; font-weight: 500; color: var(--text); padding-left: 4px; }
    .project-row__cog {
      background: none; border: 1px solid transparent; border-radius: var(--radius);
      cursor: pointer; padding: 6px; color: var(--text-subtle);
      display: flex; align-items: center; justify-content: center;
    }
    .project-row__cog:hover { background: var(--bg-muted); color: var(--text-muted); }
    .project-row__cog svg { width: 16px; height: 16px; fill: none; stroke: currentColor;
      stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
  </style>
</head>
<body>
  <div class="page">
    {% if agent_ids %}
    <div class="landing-header">
      <h1>Projects</h1>
      <a href="/create" class="btn btn-primary">Create</a>
    </div>
    <div class="project-list">
      {% for agent_id in agent_ids %}
      <div class="project-row"
           style="--workspace-accent: {{ agent_accents.get(agent_id | string, 'oklch(65% 0.15 230)') }};"
           data-agent-id="{{ agent_id }}"
           onclick="window.location='/goto/{{ agent_id }}/'">
        <span class="project-row__name">{{ agent_names.get(agent_id | string, agent_id) }}</span>
        <button class="project-row__cog"
                onclick="event.stopPropagation(); window.location='/workspace/{{ agent_id }}/settings'"
                title="Settings">
          <svg viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
        </button>
      </div>
      {% endfor %}
    </div>
    {% else %}
      {% if is_discovering %}
    <div style="display: flex; align-items: center; justify-content: center; min-height: 80vh;">
      <p class="empty-state">Discovering agents...</p>
    </div>
    <script>setTimeout(function() { location.reload(); }, 2000);</script>
      {% else %}
    <div style="text-align: center; padding: 48px 0;">
      <p class="empty-state" style="padding: 0 0 24px;">No projects yet</p>
      <a href="/create" class="btn btn-primary">Create</a>
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
  <style>"""
    + TOKENS
    + """
  </style>
</head>
<body>
  <div class="page">
    <div class="page-header">
      <a href="/" class="page-header__back">&larr; Back to projects</a>
      <button type="submit" form="create-form" class="btn btn-primary">Create</button>
    </div>
    <form id="create-form" action="/create" method="post">
      <div class="form-group">
        <div class="form-label">
          <label for="agent_name">Name</label>
        </div>
        <div class="form-input">
          <input type="text" class="input" id="agent_name" name="agent_name" value="{{ agent_name }}"
                 placeholder="selene" required>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="git_url">Repository</label>
          <p class="form-help">Git URL or local path</p>
        </div>
        <div class="form-input">
          <input type="text" class="input" id="git_url" name="git_url" value="{{ git_url }}"
                 placeholder="https://github.com/user/repo.git" required>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="branch">Branch</label>
          <p class="form-help">Leave empty for default</p>
        </div>
        <div class="form-input">
          <input type="text" class="input" id="branch" name="branch" value="{{ branch }}"
                 placeholder="main">
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="launch_mode">Launch mode</label>
          <p class="form-help">Local: Docker. Dev: this host.</p>
        </div>
        <div class="form-input">
          <select id="launch_mode" name="launch_mode" class="input">
            {% for mode in launch_modes %}
            <option value="{{ mode.value }}"{% if mode.value == selected_launch_mode %} selected{% endif %}>{{ mode.value | lower }}</option>
            {% endfor %}
          </select>
        </div>
      </div>
      <div class="form-group">
        <div class="form-label">
          <label for="include_env_file">Include .env file</label>
          <p class="form-help">Ships a local ".env" to the agent host. Ignored for git URLs.</p>
        </div>
        <div class="form-input">
          <input type="checkbox" id="include_env_file" name="include_env_file" value="1" checked>
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
  <title>Creating your project...</title>
  <style>"""
    + TOKENS
    + """
    .logs {
      margin-top: 16px; padding: 12px;
      background: var(--bg-chrome); color: var(--text-chrome);
      font-family: var(--font-mono); font-size: var(--fs-xs);
      border-radius: var(--radius-lg);
      max-height: 420px; overflow-y: auto; white-space: pre-wrap;
      border: 1px solid var(--bg-chrome);
    }
  </style>
</head>
<body class="page-workspace" style="--workspace-accent: {{ accent }};">
  <div class="page">
    <h1 class="display">Creating your project</h1>
    <p class="subtitle">This usually takes 10-30 seconds.</p>
    <p id="status" style="margin: 16px 0;"><span class="spinner"></span> <span id="status-text">{{ status_text }}</span></p>
    <div id="logs" class="logs"></div>
  </div>
  <script>
    const agentId = '{{ agent_id }}';
    const logsEl = document.getElementById('logs');
    const statusTextEl = document.getElementById('status-text');
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
            statusTextEl.textContent = 'Done. Redirecting...';
            window.location.href = data.redirect_url;
          } else if (data.status === 'FAILED') {
            statusTextEl.textContent = 'Failed: ' + (data.error || 'unknown error');
            document.getElementById('status').classList.add('error-text');
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
  <style>"""
    + TOKENS
    + """
    body { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .login-card { max-width: 420px; width: 100%; padding: 32px; margin: 16px; }
  </style>
</head>
<body>
  <div class="card login-card">
    <h1>Sign in to Minds</h1>
    <p class="subtitle" style="margin-top: 6px;">Use the login URL printed in the terminal.</p>
    <p class="muted">
      Each login URL can only be used once. If you've already used yours, restart the server to
      generate a new one.
    </p>
  </div>
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


_AUTH_ERROR_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Authentication Error</title>
  <style>"""
    + TOKENS
    + """
    body { display: flex; align-items: center; justify-content: center; min-height: 100vh; }
    .auth-error-card { max-width: 460px; width: 100%; padding: 32px; margin: 16px; }
  </style>
</head>
<body>
  <div class="card auth-error-card">
    <h1>Authentication Failed</h1>
    <p class="muted" style="margin-top: 8px;">{{ message }}</p>
    <p class="muted" style="margin-top: 8px;">
      Each login URL can only be used once. Please use the login URL printed in the terminal where
      the server is running, or restart the server to generate a new one.
    </p>
  </div>
</body>
</html>"""
)


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
    agent_accents = {str(aid): workspace_accent(str(aid)) for aid in accessible_agent_ids}
    template = _JINJA_ENV.from_string(_LANDING_PAGE_TEMPLATE)
    return template.render(
        agent_ids=accessible_agent_ids,
        agent_accents=agent_accents,
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
        "DONE": "Done. Redirecting...",
        "FAILED": "Failed: {}".format(info.error or "unknown error"),
    }
    status_text = status_text_map.get(str(info.status), "Working...")
    template = _JINJA_ENV.from_string(_CREATING_PAGE_TEMPLATE)
    return template.render(
        agent_id=agent_id,
        status_text=status_text,
        accent=workspace_accent(str(agent_id)),
    )


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


# -- Chrome (persistent shell) templates --

_CHROME_TITLEBAR_HEIGHT: Final[int] = 38


_CHROME_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Minds</title>
<style>"""
    + TOKENS
    + """
html, body { overflow: hidden; background: var(--bg-chrome); }

#minds-titlebar {
  position: fixed; top: 0; left: 0; right: 0;
  height: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
  background: var(--bg-chrome);
  display: flex; align-items: center;
  user-select: none;
  -webkit-app-region: drag;
  z-index: 100;
  border-bottom: 1px solid var(--border-chrome);
  box-shadow: var(--shadow-seam);
  padding: 0 4px;
}
{% if is_mac %}#minds-titlebar { padding-left: 72px; }{% endif %}

#minds-titlebar button {
  -webkit-app-region: no-drag;
  background: none; border: none; color: var(--text-chrome-muted); cursor: pointer;
  width: 32px; height: 28px;
  display: flex; align-items: center; justify-content: center;
  border-radius: var(--radius-sm); font-size: var(--fs-md); line-height: 1;
}
#minds-titlebar button:hover { color: var(--text-chrome); background: var(--bg-chrome-hover); }
#minds-titlebar button:active { background: var(--bg-chrome-active); }
#minds-titlebar svg {
  width: 16px; height: 16px; fill: none; stroke: currentColor;
  stroke-width: 2; stroke-linecap: round; stroke-linejoin: round;
}

.minds-nav { display: flex; gap: 2px; }
.minds-title-area {
  flex: 1; display: flex; align-items: center; justify-content: center;
  gap: 8px; padding: 0 8px; min-width: 0;
}
.minds-title-swatch {
  display: none; width: 10px; height: 10px; border-radius: 2px;
  background: var(--workspace-accent, oklch(65% 0.15 230));
  flex-shrink: 0;
}
.minds-title-swatch.visible { display: inline-block; }
.minds-title {
  color: var(--text-chrome); font-size: var(--fs-xs);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.minds-user-area { position: relative; -webkit-app-region: no-drag; flex-shrink: 0; }
.minds-user-btn {
  width: auto !important; height: auto !important; display: inline-block !important;
  color: var(--text-chrome-muted); cursor: pointer; padding: 4px 10px;
  border-radius: var(--radius-sm);
  font-size: var(--fs-xs); font-family: inherit; white-space: nowrap;
}
.minds-user-btn:hover { background: var(--bg-chrome-hover); color: var(--text-chrome); }

.minds-wc { display: flex; }
{% if is_mac %}.minds-wc { display: none; }{% endif %}
.minds-wc button { border-radius: 0; width: 36px; height: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px; }
.minds-wc button:hover { background: var(--bg-chrome-hover); border-radius: 0; }
.minds-wc button:last-child:hover { background: #dc2626; color: white; border-radius: 0; }

/* Sidebar (browser mode) */
#sidebar-panel {
  position: fixed; left: 0; top: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
  width: 260px; height: calc(100% - """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px);
  background: var(--bg-chrome); z-index: 50;
  box-shadow: 4px 0 12px rgba(0,0,0,0.3);
  transform: translateX(-100%);
  transition: transform 200ms ease-in-out;
  overflow-y: auto; padding: 0;
  border-right: 1px solid var(--border-chrome);
}
#sidebar-panel.sidebar-visible { transform: translateX(0); }

.sidebar-item {
  position: relative;
  padding: 10px 12px 10px 16px;
  cursor: pointer; font-size: var(--fs-sm); font-weight: 500;
  color: var(--text-chrome); margin: 2px 6px; border-radius: var(--radius);
  transition: background 100ms;
}
.sidebar-item::before {
  content: ""; position: absolute; left: 4px; top: 8px; bottom: 8px; width: 3px;
  border-radius: 2px; background: var(--workspace-accent, oklch(65% 0.15 230));
  opacity: 0.55;
}
.sidebar-item:hover { background: var(--bg-chrome-hover); }
.sidebar-empty {
  padding: 24px 16px; font-size: var(--fs-sm); color: var(--text-chrome-muted); text-align: center;
}

/* Content area (browser mode). Inset 6px so the chrome color frames it. */
#content-frame {
  position: fixed; left: 6px; top: """
    + str(_CHROME_TITLEBAR_HEIGHT)
    + """px;
  width: calc(100% - 12px); height: calc(100% - """
    + str(_CHROME_TITLEBAR_HEIGHT + 6)
    + """px);
  border: none;
  border-radius: var(--radius-lg);
  background: var(--bg-page);
  box-shadow: 0 1px 0 rgba(255,255,255,0.05) inset;
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
      <svg viewBox="0 0 24 24"><path d="M3 12L12 3l9 9"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/></svg>
    </button>
    <button id="back-btn" title="Back">
      <svg viewBox="0 0 24 24"><polyline points="15 18 9 12 15 6"/></svg>
    </button>
    <button id="forward-btn" title="Forward">
      <svg viewBox="0 0 24 24"><polyline points="9 6 15 12 9 18"/></svg>
    </button>
  </div>
  <div class="minds-title-area">
    <span class="minds-title-swatch" id="title-swatch"></span>
    <span class="minds-title" id="page-title">Minds</span>
  </div>
  <div class="minds-user-area">
    <button id="user-btn" class="minds-user-btn" title="Account">Log in</button>
  </div>
  <button id="requests-toggle" title="Requests" style="position:relative;">
    <svg viewBox="0 0 24 24"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
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

// Stable agent id -> OKLCH accent. Mirrors workspace_accent() in templates.py:
// SHA-256 first 4 bytes mod 360 gives the hue; fixed L and C match the
// Python side so the client and server pick the exact same color.
async function accentForAgentId(agentId) {
  var enc = new TextEncoder().encode(agentId);
  var digest = await crypto.subtle.digest('SHA-256', enc);
  var view = new DataView(digest);
  var hue = view.getUint32(0, false) % 360;
  return 'oklch(65% 0.15 ' + hue + ')';
}

var accentCache = {};
function getAccent(agentId, cb) {
  if (accentCache[agentId] !== undefined) { cb(accentCache[agentId]); return; }
  accentForAgentId(agentId).then(function(c) { accentCache[agentId] = c; cb(c); });
}

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
  navigateContent('/goto/' + agentId + '/');
  if (isElectron) {
    sidebarOpen = false;
  } else {
    sidebarOpen = false;
    document.getElementById('sidebar-panel').classList.remove('sidebar-visible');
  }
}

// -- Titlebar per-project swatch --
var currentTitleAgentId = null;
function applyTitleSwatch(agentId) {
  var swatch = document.getElementById('title-swatch');
  if (!agentId) {
    swatch.classList.remove('visible');
    document.documentElement.style.removeProperty('--workspace-accent');
    currentTitleAgentId = null;
    return;
  }
  currentTitleAgentId = agentId;
  getAccent(agentId, function(c) {
    if (currentTitleAgentId !== agentId) return;
    document.documentElement.style.setProperty('--workspace-accent', c);
    swatch.classList.add('visible');
  });
}

// -- Button handlers --
document.getElementById('sidebar-toggle').onclick = toggleSidebar;
document.getElementById('home-btn').onclick = function() { navigateContent('/'); };
document.getElementById('back-btn').onclick = goBack;
document.getElementById('forward-btn').onclick = goForward;

if (isElectron) {
  document.getElementById('min-btn').onclick = function() { window.minds.minimize(); };
  document.getElementById('max-btn').onclick = function() { window.minds.maximize(); };
  document.getElementById('close-btn').onclick = function() { window.minds.close(); };
  document.getElementById('content-frame').style.display = 'none';
  document.getElementById('sidebar-panel').style.display = 'none';
}

// -- Title tracking + auth refresh on navigation --
function refreshAuthStatus() {
  fetch('/auth/api/status').then(function(r) { return r.json(); }).then(updateAuthUI).catch(function() {});
}

if (isElectron) {
  if (window.minds.onWindowTitleChange) {
    window.minds.onWindowTitleChange(function(title) {
      document.getElementById('page-title').textContent = title || 'Minds';
    });
  } else {
    window.minds.onContentTitleChange(function(title) {
      document.getElementById('page-title').textContent = title || 'Minds';
    });
  }
  window.minds.onContentURLChange(function(url) {
    refreshAuthStatus();
    // Pull agent id out of /goto/{agentId}/... to show the titlebar swatch.
    try {
      var u = new URL(url);
      var m = u.pathname.match(/^\\/goto\\/([^/]+)/);
      applyTitleSwatch(m ? m[1] : null);
    } catch (e) {}
  });
  if (window.minds.onCurrentWorkspaceChanged) {
    window.minds.onCurrentWorkspaceChanged(function(agentId) {
      applyTitleSwatch(agentId || null);
    });
  }
} else {
  setInterval(function() {
    try {
      var t = document.getElementById('content-frame').contentDocument.title;
      if (t) document.getElementById('page-title').textContent = t;
      var loc = document.getElementById('content-frame').contentWindow.location.pathname;
      var m = loc.match(/^\\/goto\\/([^/]+)/);
      applyTitleSwatch(m ? m[1] : null);
    } catch(e) {}
  }, 500);
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

document.getElementById('requests-toggle').onclick = function() {
  if (isElectron) window.minds.toggleRequestsPanel();
};

// -- SSE for workspace list (browser mode sidebar) --
function renderWorkspaces(workspaces) {
  var container = document.getElementById('sidebar-workspaces');
  container.textContent = '';
  if (!workspaces || workspaces.length === 0) {
    var empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No projects';
    container.appendChild(empty);
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
  keys.forEach(function(key) {
    var header = document.createElement('div');
    header.style.cssText = 'padding:8px 12px 2px;font-size:11px;color:var(--text-chrome-muted);letter-spacing:0.3px;';
    header.textContent = key === 'Private' ? 'PRIVATE' : key;
    container.appendChild(header);
    groups[key].forEach(function(w) {
      var row = document.createElement('div');
      row.className = 'sidebar-item';
      row.textContent = w.name || w.id;
      row.setAttribute('data-agent-id', w.id);
      if (typeof w.accent === 'string') {
        row.style.setProperty('--workspace-accent', w.accent);
      } else {
        getAccent(w.id, function(c) { row.style.setProperty('--workspace-accent', c); });
      }
      row.addEventListener('click', function() { selectWorkspace(w.id); });
      container.appendChild(row);
    });
  });
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


_SIDEBAR_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Projects</title>
<style>"""
    + TOKENS
    + """
body { background: var(--bg-chrome); overflow-y: auto; }

h2.sidebar-heading {
  font-size: var(--fs-md); color: var(--text-chrome); padding: 12px;
  margin: 0; border-top: none; border-bottom: 1px solid var(--border-chrome); font-weight: 500;
}

.sidebar-group-label {
  padding: 8px 12px 2px; font-size: 11px; color: var(--text-chrome-muted); letter-spacing: 0.3px;
}

.sidebar-item {
  position: relative;
  padding: 10px 36px 10px 16px;
  cursor: pointer; font-size: var(--fs-sm); font-weight: 500;
  color: var(--text-chrome); border-radius: var(--radius); margin: 2px 6px;
  transition: background 100ms;
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
}
.sidebar-item::before {
  content: ""; position: absolute; left: 4px; top: 8px; bottom: 8px; width: 3px;
  border-radius: 2px; background: var(--workspace-accent, oklch(65% 0.15 230));
  opacity: 0.55;
}
.sidebar-item.is-current::before { opacity: 1; }
.sidebar-item.is-current { background: var(--bg-chrome-hover); }
.sidebar-item:hover { background: var(--bg-chrome-hover); }
.sidebar-item:hover::before { opacity: 1; }

.sidebar-item-label {
  flex: 1; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

.sidebar-open-new {
  display: none;
  background: none; border: none; padding: 4px; cursor: pointer;
  color: var(--text-chrome-muted); border-radius: var(--radius-sm);
  align-items: center; justify-content: center;
}
.sidebar-open-new:hover { color: var(--text-chrome); background: var(--bg-chrome-hover); }
.sidebar-open-new svg { width: 14px; height: 14px; fill: none; stroke: currentColor;
  stroke-width: 2; stroke-linecap: round; stroke-linejoin: round; }
.sidebar-item:hover .sidebar-open-new { display: inline-flex; }
.sidebar-item.is-current .sidebar-open-new { display: none !important; }

.sidebar-empty {
  padding: 24px 16px; font-size: var(--fs-sm); color: var(--text-chrome-muted); text-align: center;
}
</style>
</head>
<body>
<h2 class="sidebar-heading">Projects</h2>
<div id="sidebar-workspaces">
  <div class="sidebar-empty">No projects</div>
</div>
<script>
var isElectron = !!window.minds;
var currentWorkspaceId = null;
var lastWorkspaces = [];
var accentCache = {};

// Mirrors workspace_accent() in templates.py. See comment there for why
// OKLCH + fixed L/C + SHA-256-derived hue.
async function accentForAgentId(agentId) {
  var enc = new TextEncoder().encode(agentId);
  var digest = await crypto.subtle.digest('SHA-256', enc);
  var view = new DataView(digest);
  var hue = view.getUint32(0, false) % 360;
  return 'oklch(65% 0.15 ' + hue + ')';
}
function getAccent(agentId, cb) {
  if (accentCache[agentId] !== undefined) { cb(accentCache[agentId]); return; }
  accentForAgentId(agentId).then(function(c) { accentCache[agentId] = c; cb(c); });
}

function selectWorkspace(agentId) {
  if (isElectron) window.minds.navigateContent('/goto/' + agentId + '/');
}

function openInNewWindow(agentId) {
  if (isElectron && window.minds.openWorkspaceInNewWindow) {
    window.minds.openWorkspaceInNewWindow(agentId);
  }
}

function buildOpenNewBtn(agentId) {
  var btn = document.createElement('button');
  btn.className = 'sidebar-open-new';
  btn.title = 'Open in new window';
  btn.tabIndex = -1;
  btn.setAttribute('data-open-new', agentId);
  btn.innerHTML =
    '<svg viewBox="0 0 24 24"><path d="M14 3h7v7"/>' +
    '<path d="M10 14L21 3"/>' +
    '<path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/></svg>';
  return btn;
}

function renderWorkspaces(workspaces) {
  var container = document.getElementById('sidebar-workspaces');
  container.textContent = '';
  if (!workspaces || workspaces.length === 0) {
    var empty = document.createElement('div');
    empty.className = 'sidebar-empty';
    empty.textContent = 'No projects';
    container.appendChild(empty);
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
  keys.forEach(function(key) {
    var header = document.createElement('div');
    header.className = 'sidebar-group-label';
    header.textContent = key === 'Private' ? 'PRIVATE' : key;
    container.appendChild(header);
    groups[key].forEach(function(w) {
      var row = document.createElement('div');
      var isCurrent = w.id === currentWorkspaceId;
      row.className = 'sidebar-item' + (isCurrent ? ' is-current' : '');
      row.setAttribute('data-agent-id', w.id);
      var label = document.createElement('span');
      label.className = 'sidebar-item-label';
      label.textContent = w.name || w.id;
      row.appendChild(label);
      row.appendChild(buildOpenNewBtn(w.id));
      if (typeof w.accent === 'string') {
        row.style.setProperty('--workspace-accent', w.accent);
      } else {
        getAccent(w.id, function(c) { row.style.setProperty('--workspace-accent', c); });
      }
      container.appendChild(row);
    });
  });
}

function handleRowClick(target) {
  var row = target.closest('.sidebar-item');
  if (!row) return;
  var openNewBtn = target.closest('.sidebar-open-new');
  var agentId = row.getAttribute('data-agent-id');
  if (!agentId) return;
  if (openNewBtn) { openInNewWindow(agentId); return; }
  selectWorkspace(agentId);
}

document.addEventListener('click', function(e) { handleRowClick(e.target); });

document.addEventListener('contextmenu', function(e) {
  var row = e.target.closest('.sidebar-item');
  if (!row) return;
  var agentId = row.getAttribute('data-agent-id');
  if (!agentId) return;
  if (agentId === currentWorkspaceId) { e.preventDefault(); return; }
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


# -- Sharing editor, workspace settings, accounts --

_ASSOCIATE_SNIPPET: Final[str] = """
    <div class="card" style="margin: 12px 0;">
      <p style="font-weight: 500; margin-bottom: 8px;">
        This workspace needs to be associated with an account before sharing can be configured.
      </p>
      {% if accounts %}
      <form method="POST" action="/workspace/{{ agent_id }}/associate"
            style="display: flex; gap: 8px; align-items: center; margin-top: 8px;">
        <select name="user_id" class="input" style="width: auto;">
          {% for acct in accounts %}
          <option value="{{ acct.user_id }}">{{ acct.email }}</option>
          {% endfor %}
        </select>
        {% if redirect_url %}<input type="hidden" name="redirect" value="{{ redirect_url }}">{% endif %}
        <button type="submit" class="btn btn-primary">Associate</button>
      </form>
      {% else %}
      <p style="margin-top: 8px;"><a href="/auth/login">Sign in or create an account</a> to enable sharing.</p>
      {% endif %}
    </div>
"""


_SHARING_EDITOR_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>{{ title }}</title>
  <style>"""
    + TOKENS
    + """
    .acl-row { display: flex; align-items: center; justify-content: space-between;
      padding: 8px 12px; border: 1px solid var(--border); border-radius: var(--radius); margin: 4px 0;
      background: var(--bg-surface); }
    .acl-added { background: var(--success-bg); border-color: var(--success-border); }
    .acl-removed { background: var(--danger-bg); border-color: var(--danger-border); text-decoration: line-through; }
    .acl-email { font-size: var(--fs-sm); color: var(--text); }
    .acl-prefix { font-weight: 600; margin-right: 6px; font-size: var(--fs-md); }
    .acl-prefix-add { color: var(--success); }
    .acl-prefix-remove { color: var(--danger); }
    .acl-x { background: none; border: none; cursor: pointer; color: var(--text-subtle);
      font-size: var(--fs-lg); line-height: 1; padding: 0 4px; }
    .acl-x:hover { color: var(--text-muted); }
  </style>
</head>
<body class="page-workspace" style="--workspace-accent: {{ accent }};">
  <div class="page">
    <h1 id="page-heading" class="display">Share <code>{{ service_name }}</code>
      in <a href="/goto/{{ agent_id }}/">{{ ws_name or agent_id }}</a>
      {% if account_email %}(<a href="/accounts">{{ account_email }}</a>){% endif %}?</h1>

    {% if not has_account %}
    """
    + _ASSOCIATE_SNIPPET
    + """
    {% if is_request %}
    <form method="POST" action="/requests/{{ request_id }}/deny" style="margin-top: 8px;">
      <button type="submit" class="btn btn-danger">Deny request</button>
    </form>
    {% endif %}
    {% else %}

    <div id="sharing-editor">
      <p class="muted" id="loading-state" style="padding: 16px 0;">Loading...</p>
    </div>

    <div id="editor-content" style="display: none;">
      <div id="url-section" style="display: none; margin-bottom: 16px;">
        <p style="font-weight: 500; margin-bottom: 4px;">Shared URL</p>
        <div class="url-box">
          <input type="text" id="share-url" readonly onclick="this.select()">
          <button class="btn btn-secondary" onclick="copyUrl()" id="copy-btn">Copy</button>
        </div>
      </div>

      <h2 class="tight">Access List</h2>
      <div id="email-list"></div>
      <div class="input-row" style="margin-top: 8px;">
        <input type="email" class="input" id="new-email" placeholder="Add email address"
          onkeydown="if (event.key === 'Enter') { event.preventDefault(); addEmail(); }">
        <button class="btn btn-secondary" onclick="addEmail()">Add</button>
      </div>

      <div class="actions" id="action-buttons" style="justify-content: space-between;">
        {% if is_request %}
        <button class="btn btn-danger" id="deny-btn" onclick="submitDeny()">Deny</button>
        {% else %}
        <div style="display: flex; gap: 8px;">
          <button class="btn btn-secondary"
                  onclick="window.location='/workspace/{{ agent_id }}/settings'">Cancel</button>
          <button class="btn btn-danger" id="disable-btn" onclick="submitDisable()" style="display: none;">
            Disable Sharing
          </button>
        </div>
        {% endif %}
        <button class="btn btn-success" id="action-btn" onclick="submitUpdate()">Update</button>
      </div>
      <div id="submit-spinner" style="display: none; padding: 16px 0;">
        <span class="muted">Saving changes...</span>
      </div>
    </div>
  </div>

  <script>
  var proposedEmails = {{ initial_emails | tojson }};
  var serviceName = {{ service_name | tojson }};
  var agentId = {{ agent_id | tojson }};
  var isRequest = {{ is_request | tojson }};
  var requestId = {{ request_id | tojson }};
  var wsName = {{ (ws_name or agent_id) | tojson }};
  var accountEmail = {{ (account_email or '') | tojson }};

  function setHeading(isEnabled) {
    // Rebuild the heading via DOM so none of the dynamic values ever land in innerHTML.
    var h = document.getElementById('page-heading');
    if (!h) return;
    h.textContent = '';

    h.appendChild(document.createTextNode(isEnabled ? '' : 'Share '));

    var codeEl = document.createElement('code');
    codeEl.textContent = serviceName;
    h.appendChild(codeEl);

    h.appendChild(document.createTextNode(isEnabled ? ' shared in ' : ' in '));

    var link = document.createElement('a');
    link.href = '/goto/' + agentId + '/';
    link.textContent = wsName;
    h.appendChild(link);

    if (accountEmail) {
      h.appendChild(document.createTextNode(' ('));
      var acctLink = document.createElement('a');
      acctLink.href = '/accounts';
      acctLink.textContent = accountEmail;
      h.appendChild(acctLink);
      h.appendChild(document.createTextNode(')'));
    }

    if (!isEnabled) h.appendChild(document.createTextNode('?'));
  }

  // Three-state ACL: existing (already on server), added (proposed new),
  // removed (proposed removal). Every email is rendered via textContent/dataset,
  // never string-concatenated into HTML, so a maliciously crafted email
  // (e.g. from a sharing request) cannot inject script.
  var existing = [];
  var added = [];
  var removed = [];

  function createAclRow(email, variant) {
    var row = document.createElement('div');
    row.className = 'acl-row' + (variant === 'added' ? ' acl-added' : variant === 'removed' ? ' acl-removed' : '');

    var left = document.createElement('span');
    if (variant === 'added' || variant === 'removed') {
      var prefix = document.createElement('span');
      prefix.className = 'acl-prefix ' + (variant === 'added' ? 'acl-prefix-add' : 'acl-prefix-remove');
      prefix.textContent = variant === 'added' ? '+' : '\\u2212';
      left.appendChild(prefix);
    }
    var emailEl = document.createElement('span');
    emailEl.className = 'acl-email' + (variant === 'removed' ? ' muted' : '');
    emailEl.textContent = email;
    left.appendChild(emailEl);
    row.appendChild(left);

    var btn = document.createElement('button');
    btn.className = 'acl-x';
    btn.setAttribute('aria-label', 'Remove');
    btn.setAttribute('data-action', variant === 'added' ? 'unmark-added'
      : variant === 'removed' ? 'unmark-removed' : 'mark-removed');
    btn.dataset.email = email;
    btn.innerHTML = '&times;';
    row.appendChild(btn);

    return row;
  }

  function renderACL() {
    var container = document.getElementById('email-list');
    container.textContent = '';

    var rowCount = 0;
    existing.forEach(function(e) {
      if (removed.indexOf(e) >= 0) return;
      container.appendChild(createAclRow(e, 'existing'));
      rowCount++;
    });
    added.forEach(function(e) {
      container.appendChild(createAclRow(e, 'added'));
      rowCount++;
    });
    removed.forEach(function(e) {
      container.appendChild(createAclRow(e, 'removed'));
      rowCount++;
    });

    if (rowCount === 0) {
      var empty = document.createElement('p');
      empty.className = 'subtle';
      empty.style.fontSize = 'var(--fs-sm)';
      empty.textContent = 'No one in the access list';
      container.appendChild(empty);
    }
  }

  document.addEventListener('click', function(event) {
    var btn = event.target.closest('.acl-x');
    if (!btn) return;
    var action = btn.getAttribute('data-action');
    var email = btn.dataset.email;
    if (!action || !email) return;
    if (action === 'mark-removed') markRemoved(email);
    else if (action === 'unmark-added') unmarkAdded(email);
    else if (action === 'unmark-removed') unmarkRemoved(email);
  });

  function addEmail() {
    var input = document.getElementById('new-email');
    var email = input.value.trim();
    if (!email) return;
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
    fetch('/sharing/' + agentId + '/' + serviceName + '/enable', { method: 'POST', body: form })
      .then(function(r) { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
      .catch(function(err) { alert('Failed: ' + err.message); setSubmitting(false); });
  }

  function submitDisable() {
    setSubmitting(true);
    fetch('/sharing/' + agentId + '/' + serviceName + '/disable', { method: 'POST' })
      .then(function(r) { window.location.href = '/sharing/' + agentId + '/' + serviceName; })
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

  fetch('/api/sharing-status/' + agentId + '/' + serviceName)
    .then(function(r) { return r.json(); })
    .then(function(data) {
      document.getElementById('loading-state').style.display = 'none';
      document.getElementById('editor-content').style.display = 'block';

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
        existing = serverEmails;
        document.getElementById('action-btn').textContent = 'Update';
        setHeading(true);
        if (data.url) {
          document.getElementById('url-section').style.display = 'block';
          document.getElementById('share-url').value = data.url;
        }
        var disableBtn = document.getElementById('disable-btn');
        if (disableBtn) disableBtn.style.display = 'inline-flex';
      } else {
        serverEmails.forEach(function(e) {
          if (added.indexOf(e) < 0) added.push(e);
        });
        document.getElementById('action-btn').textContent = 'Share';
        setHeading(false);
      }

      proposedEmails.forEach(function(e) {
        if (existing.indexOf(e) < 0 && added.indexOf(e) < 0) {
          added.push(e);
        }
      });

      renderACL();
    })
    .catch(function(err) {
      var state = document.getElementById('loading-state');
      state.textContent = 'Failed to load sharing status: ' + err.message;
      state.className = 'error-text';
      document.getElementById('editor-content').style.display = 'block';
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
    service_name: str,
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
        service_name=service_name,
        initial_emails=initial_emails or [],
        is_request=is_request,
        request_id=request_id,
        has_account=has_account,
        accounts=accounts or [],
        redirect_url=redirect_url,
        ws_name=ws_name,
        account_email=account_email,
        accent=workspace_accent(agent_id),
    )


_WORKSPACE_SETTINGS_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Settings: {{ ws_name }}</title>
  <style>"""
    + TOKENS
    + """
  </style>
</head>
<body class="page-workspace" style="--workspace-accent: {{ accent }};">
  <div class="page">
    <h1>{{ ws_name }}</h1>
    <p class="subtitle">{{ agent_id }}</p>

    <h2 class="tight">Account</h2>
    <div id="account-section">
    {% if current_account %}
    <p>Associated with: <strong>{{ current_account.email }}</strong></p>
    <p class="notice notice-warn" style="margin: 12px 0;">
      Disassociating will remove all sharing (tunnels) for this workspace.
      You will need to set up sharing again after re-associating.
    </p>
    <button class="btn btn-danger" id="disassociate-btn" onclick="submitDisassociate()">Disassociate</button>
    <span id="disassociate-spinner" class="muted" style="display: none; margin-left: 8px;">Disassociating...</span>
    {% else %}
    """
    + _ASSOCIATE_SNIPPET
    + """
    {% endif %}
    </div>

    <h2>Sharing</h2>
    {% for server in servers %}
    <div class="card card-row">
      <span style="font-weight: 500;">{{ server }}</span>
      <a href="/sharing/{{ agent_id }}/{{ server }}" class="btn btn-secondary">Manage sharing</a>
    </div>
    {% else %}
    <p class="muted">No servers discovered for this workspace.</p>
    {% endfor %}

    {% if telegram_section %}
    <h2>Telegram</h2>
    {{ telegram_section | safe }}
    {% endif %}

    <div style="margin-top: 32px;"><a href="/">&larr; Back to projects</a></div>
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
        accent=workspace_accent(agent_id),
    )


_ACCOUNTS_PAGE_TEMPLATE: Final[str] = (
    """<!DOCTYPE html>
<html>
<head>
  <title>Manage Accounts</title>
  <style>"""
    + TOKENS
    + """
  </style>
</head>
<body>
  <div class="page">
    <h1>Manage Accounts</h1>

    {% if accounts %}
    {% for acct in accounts %}
    <div class="card card-row">
      <div>
        <div style="font-weight: 500;">{{ acct.email }}</div>
        <div class="subtle" style="font-size: var(--fs-xs);">
          {{ acct.workspace_ids | length }} workspace(s)
          {% if acct.user_id | string == default_account_id %} &middot; Default{% endif %}
        </div>
      </div>
      <div style="display: flex; gap: 8px;">
        {% if acct.user_id | string != default_account_id %}
        <form method="POST" action="/accounts/set-default">
          <input type="hidden" name="user_id" value="{{ acct.user_id }}">
          <button type="submit" class="btn btn-secondary">Set default</button>
        </form>
        {% else %}
        <span class="btn btn-secondary" style="cursor: default; opacity: 0.6;">Default</span>
        {% endif %}
        <form method="POST" action="/accounts/{{ acct.user_id }}/logout">
          <button type="submit" class="btn btn-danger">Log out</button>
        </form>
      </div>
    </div>
    {% endfor %}
    {% else %}
    <p class="muted">No accounts logged in.</p>
    {% endif %}

    <div style="margin-top: 16px;">
      <a href="/auth/login" class="btn btn-primary">Add account</a>
    </div>
    <div style="margin-top: 16px;"><a href="/">&larr; Back to projects</a></div>
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
