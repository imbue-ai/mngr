"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a Jinja2 template that
lives under ``templates/`` in this directory. Tests call these functions
directly; the FastAPI route handlers call them the same way. Keeping the
public signatures stable lets the unit tests keep working without caring
that we moved from inline strings to file-based templates.
"""

import hashlib
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
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
    template = JINJA_ENV.get_template("landing.html")
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
    """Render the agent creation form page."""
    effective_url = git_url if git_url else _DEFAULT_GIT_URL
    effective_name = agent_name if agent_name else _DEFAULT_AGENT_NAME
    effective_branch = branch if branch else _DEFAULT_BRANCH
    template = JINJA_ENV.get_template("create.html")
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
    template = JINJA_ENV.get_template("creating.html")
    return template.render(
        agent_id=agent_id,
        status_text=status_text,
        accent=workspace_accent(str(agent_id)),
    )


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


# -- Chrome (persistent shell) templates --


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
    return JINJA_ENV.get_template("chrome.html").render(
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
    return JINJA_ENV.get_template("sidebar.html").render()


# -- Workspace/settings/sharing/accounts --


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
    return JINJA_ENV.get_template("sharing.html").render(
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
) -> str:
    """Render the manage accounts page."""
    return JINJA_ENV.get_template("accounts.html").render(
        accounts=accounts,
        default_account_id=default_account_id or "",
    )


@pure
def render_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    service: object,
    checked_permissions: Sequence[str],
) -> str:
    """Render the latchkey permission approval dialog.

    ``service`` is a ``ServicePermissionInfo``; passed as ``object`` to keep
    this module free of a downstream dependency on the latchkey package.
    The template only reads its ``display_name``, ``description``, and
    ``permission_schemas`` attributes.
    """
    return JINJA_ENV.get_template("permissions.html").render(
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        service=service,
        checked_permissions=set(checked_permissions),
        accent=workspace_accent(agent_id),
    )
