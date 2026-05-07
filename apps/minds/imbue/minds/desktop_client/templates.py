"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a Jinja2 template that
lives under ``templates/`` in this directory. Tests call these functions
directly; the FastAPI route handlers call them the same way. Keeping the
public signatures stable lets the unit tests keep working without caring
that we moved from inline strings to file-based templates.
"""

import hashlib
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

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


_DEFAULT_GIT_URL: Final[str] = os.getenv(
    "MINDS_WORKSPACE_GIT_URL", "https://github.com/imbue-ai/forever-claude-template.git"
)


_DEFAULT_AGENT_NAME: Final[str] = os.getenv("MINDS_WORKSPACE_NAME", "assistant")


_DEFAULT_BRANCH: Final[str] = os.getenv("MINDS_WORKSPACE_BRANCH", "")


@pure
def render_create_form(
    git_url: str = "",
    agent_name: str = "",
    branch: str = "",
    launch_mode: LaunchMode = LaunchMode.LOCAL,
    accounts: Sequence[object] | None = None,
    default_account_id: str = "",
) -> str:
    """Render the agent creation form page."""
    effective_url = git_url if git_url else _DEFAULT_GIT_URL
    effective_name = agent_name if agent_name else _DEFAULT_AGENT_NAME
    effective_branch = branch if branch else _DEFAULT_BRANCH
    has_account = bool(default_account_id and accounts)
    effective_mode = (
        launch_mode
        if launch_mode != LaunchMode.LOCAL
        else (LaunchMode.IMBUE_CLOUD if has_account else LaunchMode.LOCAL)
    )
    template = JINJA_ENV.get_template("create.html")
    return template.render(
        git_url=effective_url,
        agent_name=effective_name,
        branch=effective_branch,
        launch_modes=list(LaunchMode),
        selected_launch_mode=effective_mode.value,
        accounts=accounts or [],
        default_account_id=default_account_id,
    )


_STATUS_TEXT_DEFAULT: Final[dict[str, str]] = {
    "CLONING": "Cloning repository...",
    "CREATING": "Creating agent...",
    "DONE": "Done. Redirecting...",
}

_STATUS_TEXT_IMBUE_CLOUD: Final[dict[str, str]] = {
    "CLONING": "Connecting to host...",
    "CREATING": "Setting up agent...",
    "DONE": "Done. Redirecting...",
}


@pure
def render_creating_page(
    creation_id: CreationId,
    info: AgentCreationInfo,
    launch_mode: LaunchMode = LaunchMode.LOCAL,
) -> str:
    """Render the progress page shown while an agent is being created.

    The page is keyed by ``creation_id`` (minds-internal in-flight handle)
    rather than ``agent_id`` because the canonical agent id only comes
    into existence once the inner ``mngr create`` returns -- the page
    needs a stable handle to poll status from the moment the user kicks
    off the form. The template's status-poll URL still includes this id
    so SSE/log-streaming endpoints can find the right ``log_queue``.
    """
    text_map = _STATUS_TEXT_IMBUE_CLOUD if launch_mode is LaunchMode.IMBUE_CLOUD else _STATUS_TEXT_DEFAULT
    if str(info.status) == "FAILED":
        status_text = "Failed: {}".format(info.error or "unknown error")
    else:
        status_text = text_map.get(str(info.status), "Working...")
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
    is_request: bool = False,
    request_id: str = "",
    has_account: bool = True,
    accounts: Sequence[object] | None = None,
    redirect_url: str = "",
    ws_name: str = "",
    account_email: str = "",
) -> str:
    """Render the sharing editor page used for both request approval and direct editing.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the page title points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return JINJA_ENV.get_template("sharing.html").render(
        title=title,
        agent_id=agent_id,
        service_name=service_name,
        mngr_forward_origin=mngr_forward_origin,
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
