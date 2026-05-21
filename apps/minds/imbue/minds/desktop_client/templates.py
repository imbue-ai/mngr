"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a Jinja2 template that
lives under ``templates/`` in this directory. Tests call these functions
directly; the FastAPI route handlers call them the same way. Keeping the
public signatures stable lets the unit tests keep working without caring
that we moved from inline strings to file-based templates.
"""

import hashlib
import html
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.loading_page import render_loading_page

TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

JINJA_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default_for_string=True, default=True),
)


# -- Per-workspace identity color --
# See docs on workspace_accent() for why OKLCH + fixed L/C + SHA-256-derived
# hue. Mirrored on the JS side (static/chrome.js, static/sidebar.js).

# Lightness percent and chroma for the OKLCH workspace accent. Fixed across
# all workspaces so the only axis of variation is the hue.
_WORKSPACE_L: Final[int] = 65
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


# -- Page renderers --


@pure
def render_landing_page(
    accessible_agent_ids: Sequence[AgentId],
    mngr_forward_origin: str = "",
    telegram_status_by_agent_id: dict[str, bool] | None = None,
    is_discovering: bool = False,
    agent_names: dict[str, str] | None = None,
    destroying_status_by_agent_id: dict[str, str] | None = None,
) -> str:
    """Render the landing page listing accessible workspaces.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin
    (e.g. ``"http://localhost:8421"``). Workspace links target
    ``{mngr_forward_origin}/goto/<agent>/`` because Phase 2 deletes minds'
    in-process subdomain forwarder; the plugin owns ``/goto/`` now.

    telegram_status_by_agent_id maps agent ID strings to whether they have
    active Telegram bot credentials. When None, no telegram buttons are shown.

    agent_names maps agent ID strings to human-readable workspace names.

    destroying_status_by_agent_id maps agent ID strings to one of
    ``"running"``/``"failed"`` for agents whose detached destroy subprocess
    is currently in flight (running) or exited without removing the agent
    (failed). Agents whose destroy is ``done`` are not included -- the
    landing handler deletes those records so the row vanishes naturally
    once discovery propagates ``AgentDestroyed``. When None, no marker is
    shown.

    When is_discovering is True, the page shows a "Discovering agents..." message
    with auto-refresh instead of the empty state. This is used when the
    envelope-stream consumer hasn't completed initial agent discovery yet.
    """
    agent_accents = {str(aid): workspace_accent(str(aid)) for aid in accessible_agent_ids}
    template = JINJA_ENV.get_template("landing.html")
    return template.render(
        agent_ids=accessible_agent_ids,
        agent_accents=agent_accents,
        mngr_forward_origin=mngr_forward_origin,
        telegram_enabled=telegram_status_by_agent_id is not None,
        telegram_status_by_agent_id=telegram_status_by_agent_id or {},
        is_discovering=is_discovering,
        agent_names=agent_names or {},
        destroying_status_by_agent_id=destroying_status_by_agent_id or {},
    )


# Hardcoded fallbacks for the workspace-creation form. Overridable via
# the MINDS_WORKSPACE_* env vars in dev tiers ONLY -- see
# ``_dev_only_workspace_default`` for the gating rationale.
_FALLBACK_GIT_URL: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FALLBACK_HOST_NAME: Final[str] = "assistant"
_FALLBACK_BRANCH: Final[str] = ""

# Root names that map to operator-managed shared tiers (production /
# staging). For these tiers the MINDS_WORKSPACE_* env-var defaults are
# intentionally ignored: ``just minds-start`` (and any other dev-iteration
# tool) exports those vars from the operator's local FCT worktree state,
# which only makes sense when iterating against a per-developer dev env.
# In staging / production the workspaces are end-user-driven and a leaked
# ``MINDS_WORKSPACE_BRANCH`` from the operator's shell would silently
# pin the lease to a ref that no pool host carries.
_SHARED_TIER_ROOT_NAMES: Final[frozenset[str]] = frozenset({DEFAULT_MINDS_ROOT_NAME, "minds-staging"})


def _dev_only_workspace_default(env_var: str, fallback: str) -> str:
    """Read ``env_var`` for dev tiers; otherwise return ``fallback``.

    The MINDS_WORKSPACE_GIT_URL / _NAME / _BRANCH env vars are a dev
    convenience that wire the create-form's defaults to the operator's
    local FCT worktree (``just minds-start`` exports them). They have
    no business pre-filling the form in staging or production, where
    workspaces are end-user-driven and the operator's local git state
    is irrelevant.

    Activation is detected via ``MINDS_ROOT_NAME``. The env var is
    honored only when the root name names a dev tier (i.e. anything
    other than ``minds`` / ``minds-staging``). An unactivated shell --
    ``MINDS_ROOT_NAME`` unset entirely -- is treated as non-dev (defensive
    default: ``minds run`` always activates first today, so this branch
    is essentially unreachable; we still want to ignore the env var if
    we somehow get there).
    """
    root_name = os.environ.get(MINDS_ROOT_NAME_ENV_VAR, "")
    if not root_name or root_name in _SHARED_TIER_ROOT_NAMES:
        return fallback
    return os.environ.get(env_var, fallback)


@pure
def render_create_form(
    git_url: str = "",
    host_name: str = "",
    branch: str = "",
    launch_mode: LaunchMode | None = None,
    ai_provider: AIProvider | None = None,
    accounts: Sequence[object] | None = None,
    default_account_id: str = "",
    gh_token: str = "",
    anthropic_api_key: str = "",
    error_message: str = "",
) -> str:
    """Render the agent creation form page.

    The compute provider (``launch_mode``) and AI provider are independent.
    Both default to ``IMBUE_CLOUD`` when an account is selected; without
    an account we drop them to ``LOCAL`` / ``SUBSCRIPTION`` so the form
    starts in a valid state for the no-account flow.

    ``host_name`` is the value of the form's "Name" field; it drives the
    host name on the resulting workspace. (The agent itself is always
    named ``system-services``.)
    """
    effective_url = git_url if git_url else _dev_only_workspace_default("MINDS_WORKSPACE_GIT_URL", _FALLBACK_GIT_URL)
    effective_name = (
        host_name if host_name else _dev_only_workspace_default("MINDS_WORKSPACE_NAME", _FALLBACK_HOST_NAME)
    )
    effective_branch = branch if branch else _dev_only_workspace_default("MINDS_WORKSPACE_BRANCH", _FALLBACK_BRANCH)
    has_account = bool(default_account_id and accounts)
    effective_launch_mode = (
        launch_mode if launch_mode is not None else (LaunchMode.IMBUE_CLOUD if has_account else LaunchMode.LOCAL)
    )
    effective_ai_provider = (
        ai_provider
        if ai_provider is not None
        else (AIProvider.IMBUE_CLOUD if has_account else AIProvider.SUBSCRIPTION)
    )
    template = JINJA_ENV.get_template("create.html")
    return template.render(
        git_url=effective_url,
        host_name=effective_name,
        branch=effective_branch,
        launch_modes=list(LaunchMode),
        selected_launch_mode=effective_launch_mode.value,
        ai_providers=list(AIProvider),
        selected_ai_provider=effective_ai_provider.value,
        accounts=accounts or [],
        default_account_id=default_account_id,
        gh_token=gh_token,
        anthropic_api_key=anthropic_api_key,
        error_message=error_message,
    )


_STATUS_TEXT_DEFAULT: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Cloning repository...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "PROVISIONING_AI": "Provisioning AI access...",
    "CREATING_WORKSPACE": "Creating workspace...",
    "WAITING_FOR_READY": "Waiting for workspace to be ready...",
    "DONE": "Done. Redirecting...",
}

# IMBUE_CLOUD diverges in wording for the connection / agent-setup phases
# where the user-facing mental model is "connecting to / setting up an
# existing pool host" rather than "cloning / creating a new workspace".
_STATUS_TEXT_IMBUE_CLOUD: Final[dict[str, str]] = {
    "INITIALIZING": "Starting...",
    "CLONING_REPO": "Connecting to host...",
    "CHECKING_OUT_BRANCH": "Checking out branch...",
    "PROVISIONING_AI": "Provisioning AI access...",
    "CREATING_WORKSPACE": "Setting up agent...",
    "WAITING_FOR_READY": "Waiting for workspace to be ready...",
    "DONE": "Done. Redirecting...",
}


@pure
def status_text_for(
    status: str,
    error: str | None = None,
    launch_mode: LaunchMode = LaunchMode.LOCAL,
) -> str:
    """Resolve the UI caption for an ``AgentCreationStatus`` value.

    ``status`` is the stringified enum value (e.g. ``"CLONING_REPO"``).
    ``error`` is consulted only for the ``FAILED`` case so the caption
    can surface the underlying error message; for every other status the
    text comes from the mode-aware ``_STATUS_TEXT_*`` maps.
    """
    if status == "FAILED":
        return "Failed: {}".format(error or "unknown error")
    text_map = _STATUS_TEXT_IMBUE_CLOUD if launch_mode is LaunchMode.IMBUE_CLOUD else _STATUS_TEXT_DEFAULT
    return text_map.get(status, "Working...")


@pure
def render_creating_page(
    creation_id: CreationId,
    info: AgentCreationInfo,
) -> str:
    """Render the progress page shown while an agent is being created.

    The page is keyed by ``creation_id`` (minds-internal in-flight handle)
    rather than ``agent_id`` because the canonical agent id only comes
    into existence once the inner ``mngr create`` returns -- the page
    needs a stable handle to poll status from the moment the user kicks
    off the form. The template's status-poll URL still includes this id
    so SSE/log-streaming endpoints can find the right ``log_queue``.

    The launch mode is read off ``info.launch_mode`` --
    ``AgentCreator.start_creation`` records it before spawning the worker
    thread, so the ``AgentCreationInfo`` snapshot is the single source of
    truth for caption resolution (consistent with the SSE status events).
    """
    status_text = status_text_for(str(info.status), error=info.error, launch_mode=info.launch_mode)
    template = JINJA_ENV.get_template("creating.html")
    return template.render(
        agent_id=creation_id,
        status_text=status_text,
        accent=workspace_accent(str(creation_id)),
    )


@pure
def render_welcome_page() -> str:
    """Render the welcome/splash page for first-time users."""
    return JINJA_ENV.get_template("welcome.html").render()


@pure
def render_login_page() -> str:
    """Render the login prompt page for unauthenticated users."""
    return JINJA_ENV.get_template("login.html").render()


@pure
def render_login_redirect_page(one_time_code: OneTimeCode) -> str:
    """Render the JS redirect page that forwards to /authenticate."""
    return JINJA_ENV.get_template("login_redirect.html").render(one_time_code=one_time_code)


@pure
def render_auth_error_page(message: str) -> str:
    """Render an error page for failed authentication."""
    return JINJA_ENV.get_template("auth_error.html").render(message=message)


# CSS for the recovery page's restart controls, appended to the shared
# ``LOADING_PAGE_CSS``. The card itself, spinner, heading and message all come
# from the shared loading page, so the recovery page's loading state is
# byte-identical to the mngr_forward proxy loader.
_RECOVERY_STYLE: Final[str] = """\
      .hidden { display: none; }
      details {
        margin-top: 16px;
        border: 1px solid #fde68a;
        background: #fffbeb;
        border-radius: 6px;
        color: #92400e;
      }
      summary { cursor: pointer; padding: 8px 12px; font-weight: 500; font-size: 0.8125rem; }
      details pre {
        margin: 0;
        padding: 0 12px 12px;
        max-height: 240px;
        overflow-y: auto;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font-size: 0.75rem;
        line-height: 1.5;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      }
      button {
        margin-top: 16px;
        background: #18181b;
        color: #fff;
        border: 0;
        border-radius: 6px;
        padding: 8px 16px;
        font-size: 0.875rem;
        font-weight: 500;
        cursor: pointer;
      }
      button:hover { background: #3f3f46; }
"""

# The recovery page's behavior. It drives the shared loading card (toggling
# the spinner, heading and message) plus the recovery-only restart button and
# error <details>. While a restart is in flight it auto-refreshes itself:
# _handle_recovery_page re-renders from the live tracker state on every GET,
# so a timed reload is the whole "is it healthy yet?" check.
_RECOVERY_SCRIPT: Final[str] = """\
      (function () {
        var root = document.querySelector('[data-agent-id]');
        if (!root) return;
        var agentId = root.dataset.agentId;
        var returnTo = root.dataset.returnTo || '';
        var initialStatus = root.dataset.initialStatus || 'stuck';

        var titleEl = document.getElementById('loading-title');
        var messageEl = document.getElementById('loading-message');
        var spinnerEl = document.getElementById('loading-spinner');
        var errorEl = document.getElementById('recovery-error');  // null unless restart_failed
        var hostBtn = document.getElementById('recovery-host-btn');

        var REFRESH_INTERVAL_MS = 1500;

        function show(el, visible) {
          if (el) el.classList.toggle('hidden', !visible);
        }

        // The poll URL omits intent=restart so that, once the restart is
        // dispatched, a healthy tracker state 302s the user back to the workspace.
        function pollUrl() {
          var u = '/agents/' + encodeURIComponent(agentId) + '/recovery';
          if (returnTo) u += '?return_to=' + encodeURIComponent(returnTo);
          return u;
        }
        function scheduleRefresh() {
          setTimeout(function () { window.location.assign(pollUrl()); }, REFRESH_INTERVAL_MS);
        }

        function renderLoading() {
          titleEl.textContent = 'Loading workspace';
          messageEl.textContent = '';
          show(spinnerEl, true);
          show(errorEl, false);
          show(hostBtn, false);
        }
        function renderHostOffer() {
          titleEl.textContent = 'System interface not responding';
          messageEl.textContent =
            'This workspace needs a restart to recover. In-progress work in all agents will be interrupted.';
          show(spinnerEl, false);
          show(errorEl, false);
          hostBtn.textContent = 'Restart workspace';
          show(hostBtn, true);
        }
        function renderFailed() {
          titleEl.textContent = 'Restart failed';
          messageEl.textContent =
            'The restart did not recover the workspace. You can try again, or contact support '
            + 'if the problem persists.';
          show(spinnerEl, false);
          show(errorEl, true);
          hostBtn.textContent = 'Restart workspace';
          show(hostBtn, true);
        }
        function renderDispatchError() {
          titleEl.textContent = 'System interface not responding';
          messageEl.textContent = 'Could not start the restart. Check your connection and try again.';
          show(spinnerEl, false);
          show(errorEl, false);
          hostBtn.textContent = 'Restart workspace';
          show(hostBtn, true);
        }

        function postRestart(path) {
          renderLoading();
          // The endpoint returns 202 once the tracker is RESTARTING; any other
          // status means the dispatch did not start, so surface an error
          // instead of refreshing into a re-probe loop.
          fetch('/api/agents/' + encodeURIComponent(agentId) + path, {
            method: 'POST',
            credentials: 'same-origin',
          }).then(function (resp) {
            if (resp.ok) { scheduleRefresh(); } else { renderDispatchError(); }
          }, renderDispatchError);
        }

        function runProbe() {
          renderLoading();
          fetch('/api/agents/' + encodeURIComponent(agentId) + '/host-health', {
            credentials: 'same-origin',
          }).then(function (resp) {
            return resp.json();
          }).then(function (data) {
            if (data && data.reachable) {
              // Container running: the surgical system-interface restart can
              // recover the workspace without interrupting agents.
              postRestart('/restart-system-interface');
            } else if (data && data.host_offline) {
              // Container fully stopped: nothing is running, so a host restart
              // just starts it back up -- dispatch it, no confirmation needed.
              postRestart('/restart-host');
            } else {
              // Ambiguous host state: a host restart could interrupt running
              // agents, so make the user confirm by clicking.
              renderHostOffer();
            }
          }, function () {
            renderHostOffer();
          });
        }

        hostBtn.addEventListener('click', function () {
          postRestart('/restart-host');
        });

        if (initialStatus === 'restarting') {
          renderLoading();
          scheduleRefresh();
        } else if (initialStatus === 'restart_failed') {
          renderFailed();
        } else if (initialStatus === 'healthy') {
          // Degenerate: rendered HEALTHY with no return_to to 302 to. Offer a
          // manual restart rather than auto-dispatching one on a healthy page.
          renderHostOffer();
        } else {
          runProbe();
        }
      })();
"""


@pure
def render_recovery_page(
    agent_id: AgentId,
    return_to: str,
    initial_status: str,
    initial_error: str,
) -> str:
    """Render the workspace-recovery page shown when the system interface is unresponsive.

    Built on the shared ``render_loading_page`` so the recovery page's loading
    state is identical to the mngr_forward proxy loader. ``initial_status`` is
    one of ``"stuck"``/``"restarting"``/``"restart_failed"``/``"healthy"`` and
    governs the page's initial UI state. ``initial_error`` is the failure
    reason shown (collapsed) when ``initial_status`` is ``"restart_failed"``.
    ``return_to`` is the URL the page navigates back to once the workspace is
    healthy again.
    """
    error_block = ""
    if initial_error:
        error_block = (
            '      <details id="recovery-error" class="hidden">\n'
            "        <summary>Show error details</summary>\n"
            f"        <pre>{html.escape(initial_error)}</pre>\n"
            "      </details>\n"
        )
    card_extra = error_block + '      <button id="recovery-host-btn" class="hidden">Restart workspace</button>\n'
    card_attrs = (
        f' data-agent-id="{html.escape(str(agent_id))}"'
        f' data-return-to="{html.escape(return_to)}"'
        f' data-initial-status="{html.escape(initial_status)}"'
    )
    return render_loading_page(
        style_extra=_RECOVERY_STYLE,
        card_attrs=card_attrs,
        card_extra=card_extra,
        body_extra="    <script>\n" + _RECOVERY_SCRIPT + "    </script>\n",
    )


@pure
def render_destroying_page(
    agent_id: AgentId,
    agent_name: str,
    pid: int,
    status: str,
) -> str:
    """Render the detail page for an in-flight or recently-completed destroy.

    The page polls ``/api/destroying/<agent_id>/{status,log}`` to keep its
    log tail and status badge up to date; once status flips to ``done`` it
    redirects to ``/``. ``status`` is the initial server-side computed
    value (``running``/``failed``/``done``) so the page renders correctly
    even before the first poll completes.
    """
    return JINJA_ENV.get_template("destroying.html").render(
        agent_id=str(agent_id),
        agent_name=agent_name,
        pid=pid,
        status=status,
        accent=workspace_accent(str(agent_id)),
    )


# -- Chrome (persistent shell) templates --


@pure
def render_chrome_page(
    is_mac: bool = False,
    is_authenticated: bool = False,
    mngr_forward_origin: str = "",
    initial_workspaces: Sequence[dict[str, str]] | None = None,
) -> str:
    """Render the persistent chrome page (title bar + sidebar + content iframe).

    is_mac controls whether macOS-specific styling is applied (traffic light padding,
    hidden window controls).

    ``mngr_forward_origin`` is exposed to the page-level JS via a
    ``data-mngr-forward-origin`` attribute on the body so chrome.js can build
    workspace links that target the plugin's port directly.

    In Electron mode, the iframe and browser sidebar are hidden via JS; the content
    and sidebar are handled by separate WebContentsViews.
    """
    return JINJA_ENV.get_template("chrome.html").render(
        is_mac=is_mac,
        is_authenticated=is_authenticated,
        mngr_forward_origin=mngr_forward_origin,
        initial_workspaces=initial_workspaces or [],
    )


@pure
def render_sidebar_page(mngr_forward_origin: str = "") -> str:
    """Render the standalone sidebar page for the Electron sidebar WebContentsView.

    This page shows the workspace list and subscribes to SSE updates. In Electron,
    clicking a workspace sends an IPC message via the preload bridge to navigate
    the content WebContentsView. ``mngr_forward_origin`` is exposed via
    ``data-mngr-forward-origin`` so sidebar.js can build the cross-origin
    ``/goto/<agent>/`` URL the plugin serves.
    """
    return JINJA_ENV.get_template("sidebar.html").render(
        mngr_forward_origin=mngr_forward_origin,
    )


# -- Workspace/settings/sharing/accounts --


@pure
def render_sharing_editor(
    agent_id: str,
    service_name: str,
    title: str,
    mngr_forward_origin: str = "",
    initial_emails: list[str] | None = None,
    has_account: bool = True,
    accounts: Sequence[object] | None = None,
    redirect_url: str = "",
    ws_name: str = "",
    account_email: str = "",
) -> str:
    """Render the sharing editor page used by the workspace-settings sharing flow.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the page title points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return JINJA_ENV.get_template("sharing.html").render(
        title=title,
        agent_id=agent_id,
        service_name=service_name,
        mngr_forward_origin=mngr_forward_origin,
        initial_emails=initial_emails or [],
        has_account=has_account,
        accounts=accounts or [],
        redirect_url=redirect_url,
        ws_name=ws_name,
        account_email=account_email,
        accent=workspace_accent(agent_id),
    )


@pure
def render_workspace_settings(
    agent_id: str,
    ws_name: str,
    current_account: object | None,
    accounts: Sequence[object],
    servers: Sequence[str],
    telegram_state: str | None = None,
) -> str:
    """Render the workspace settings page.

    telegram_state controls whether the Telegram section is shown:

    - ``None`` -- no Telegram orchestrator configured; section is hidden.
    - ``"active"`` -- Telegram is already set up for this workspace.
    - ``"pending"`` -- setup button is shown.

    Interactivity for the setup flow lives in ``static/workspace_settings.js``,
    which reads the agent id from the page's ``data-agent-id`` attribute.
    """
    return JINJA_ENV.get_template("workspace_settings.html").render(
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
        accent=workspace_accent(agent_id),
    )


@pure
def render_accounts_page(
    accounts: Sequence[object],
    default_account_id: str | None = None,
    enabled_by_user_id: Mapping[str, bool] | None = None,
) -> str:
    """Render the manage accounts page.

    ``enabled_by_user_id`` maps each account's user_id to whether its
    ``[providers.imbue_cloud_<slug>]`` block is enabled in settings.toml.
    The template renders a "Signed out" indicator when an account is
    present (still in sessions.json) but the block has been
    auto-disabled by an observed auth error.
    """
    return JINJA_ENV.get_template("accounts.html").render(
        accounts=accounts,
        default_account_id=default_account_id or "",
        enabled_by_user_id=dict(enabled_by_user_id or {}),
    )
