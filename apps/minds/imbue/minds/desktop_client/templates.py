"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a Jinja2 template that
lives under ``templates/`` in this directory. Tests call these functions
directly; the FastAPI route handlers call them the same way. Keeping the
public signatures stable lets the unit tests keep working without caring
that we moved from inline strings to file-based templates.
"""

import hashlib
import json
import os
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from typing import Final
from typing import Protocol

from jinja2 import Environment
from jinja2 import FileSystemLoader
from jinja2 import select_autoescape
from loguru import logger

from imbue.imbue_common.pure import pure
from imbue.minds.bootstrap import DEFAULT_MINDS_ROOT_NAME
from imbue.minds.bootstrap import MINDS_ROOT_NAME_ENV_VAR
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.desktop_client.onboarding import expected_creation_duration_seconds
from imbue.minds.desktop_client.session_store import AccountSession
from imbue.minds.desktop_client.ssr_sidecar import SsrSidecar
from imbue.minds.desktop_client.ssr_sidecar import SsrSidecarError
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId

TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

JINJA_ENV: Final[Environment] = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(default_for_string=True, default=True),
)


# -- SSR helpers --
#
# Each ``render_*`` shim that's been migrated to Solid takes an optional
# ``sidecar`` and a ``route`` key + props dict. The shim attempts the SSR
# render and falls back to the client-render shell if the sidecar is
# unhealthy (or absent in unit tests). Keeping the function signatures
# stable means existing route handlers and unit tests don't change shape.


class _RendersHtml(Protocol):
    """Structural type accepted by ``_render_ssr_or_fallback``.

    Concrete production callers pass an ``SsrSidecar``. Tests pass
    lightweight fakes that don't subclass it (the production class is a
    ``MutableModel`` so subclassing is awkward); this Protocol lets the
    type checker accept both shapes without requiring an inheritance
    relationship.
    """

    def render(self, *, route: str, props: dict[str, Any], bundle: str = ...) -> str: ...


_VALID_BUNDLES: Final[frozenset[str]] = frozenset({"app", "chrome", "sidebar"})


def _client_render_shell(*, route: str, props: dict[str, Any], bundle: str = "app") -> str:
    """Inline shell HTML that boots the client bundle without SSR.

    Used when the SSR sidecar is unhealthy or in tests that don't spin up
    a Node process. The page is identical in observable behavior to the
    SSR'd version once the client hydrates, except that the user sees an
    empty ``#app`` mount point for one tick before Solid takes over.

    ``bundle`` selects which client entry to load (one of
    ``"app"``/``"chrome"``/``"sidebar"``). The Vite build emits each
    entry under a stable filename so the shim doesn't need the manifest.

    The route key and props are inlined as a JSON ``<script>`` blob; the
    matching client entry reads it on boot.
    """
    if bundle not in _VALID_BUNDLES:
        raise SsrSidecarError(f"Unknown bundle for fallback shell: {bundle!r}")
    payload = (
        json.dumps({"route": route, "props": props})
        .replace("<", "\\u003c")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )
    # All three bundles import ``globals.css`` so Vite emits one shared
    # stylesheet under ``globals.css``; loading it works whichever bundle
    # owns the page.
    return (
        "<!doctype html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="UTF-8"><title>Minds</title>\n'
        '<link rel="stylesheet" href="/_static/_dist/assets/globals.css">\n'
        "</head>\n"
        '<body class="bg-zinc-50 text-zinc-900 font-sans antialiased">\n'
        '<div id="app"></div>\n'
        f'<script type="application/json" id="__route__">{payload}</script>\n'
        f'<script type="module" src="/_static/_dist/assets/{bundle}.js"></script>\n'
        "</body>\n"
        "</html>\n"
    )


def _render_ssr_or_fallback(
    *,
    sidecar: _RendersHtml | None,
    route: str,
    props: dict[str, Any],
    bundle: str = "app",
) -> str:
    """Try SSR; fall back to the client-render shell on any failure.

    The shell is byte-identical regardless of which page is being
    rendered -- only the inlined ``{ route, props }`` payload differs --
    so tests can assert on the payload to verify behavior without
    standing up a Node sidecar.
    """
    if sidecar is None:
        return _client_render_shell(route=route, props=props, bundle=bundle)
    try:
        return sidecar.render(route=route, props=props, bundle=bundle)
    except SsrSidecarError as exc:
        logger.warning("SSR sidecar render failed for route {!r}; using fallback shell: {}", route, exc)
        return _client_render_shell(route=route, props=props, bundle=bundle)


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


def render_landing_page(
    accessible_agent_ids: Sequence[AgentId],
    mngr_forward_origin: str = "",
    telegram_status_by_agent_id: dict[str, bool] | None = None,
    is_discovering: bool = False,
    agent_names: dict[str, str] | None = None,
    destroying_status_by_agent_id: dict[str, str] | None = None,
    sidecar: SsrSidecar | None = None,
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
    props: dict[str, Any] = {
        "agent_ids": [str(aid) for aid in accessible_agent_ids],
        "agent_accents": agent_accents,
        "mngr_forward_origin": mngr_forward_origin,
        "telegram_enabled": telegram_status_by_agent_id is not None,
        "telegram_status_by_agent_id": telegram_status_by_agent_id or {},
        "is_discovering": is_discovering,
        "agent_names": agent_names or {},
        "destroying_status_by_agent_id": destroying_status_by_agent_id or {},
    }
    return _render_ssr_or_fallback(sidecar=sidecar, route="landing", props=props)


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


def _serialize_account(account: AccountSession) -> dict[str, str]:
    """Flatten an ``AccountSession`` to the JSON-serializable shape the Solid create form expects."""
    return {"user_id": str(account.user_id), "email": account.email}


def render_create_form(
    git_url: str = "",
    host_name: str = "",
    branch: str = "",
    launch_mode: LaunchMode | None = None,
    ai_provider: AIProvider | None = None,
    backup_provider: BackupProvider | None = None,
    backup_encryption_method: BackupEncryptionMethod | None = None,
    backup_api_key_env: str = "",
    has_saved_backup_password: bool = False,
    accounts: Sequence[AccountSession] | None = None,
    default_account_id: str = "",
    anthropic_api_key: str = "",
    error_message: str = "",
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the agent creation form page.

    The compute provider (``launch_mode``), AI provider, and backup provider
    are independent. The compute / AI providers default to ``IMBUE_CLOUD``
    when an account is selected; without an account they drop to ``LIMA`` /
    ``SUBSCRIPTION``. The backup provider defaults to ``IMBUE_CLOUD`` with an
    account and ``CONFIGURE_LATER`` without one. The backup encryption method
    defaults to ``NO_PASSWORD``.

    ``has_saved_backup_password`` toggles the master-password input between a
    "enter a passphrase" field (no saved password yet) and a read-only
    "a saved password will be used" indicator.

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
        launch_mode if launch_mode is not None else (LaunchMode.IMBUE_CLOUD if has_account else LaunchMode.LIMA)
    )
    effective_ai_provider = (
        ai_provider
        if ai_provider is not None
        else (AIProvider.IMBUE_CLOUD if has_account else AIProvider.SUBSCRIPTION)
    )
    effective_backup_provider = (
        backup_provider
        if backup_provider is not None
        else (BackupProvider.IMBUE_CLOUD if has_account else BackupProvider.CONFIGURE_LATER)
    )
    effective_backup_encryption = (
        backup_encryption_method if backup_encryption_method is not None else BackupEncryptionMethod.NO_PASSWORD
    )
    props: dict[str, Any] = {
        "git_url": effective_url,
        "host_name": effective_name,
        "branch": effective_branch,
        "launch_modes": [mode.value for mode in LaunchMode],
        "selected_launch_mode": effective_launch_mode.value,
        "ai_providers": [provider.value for provider in AIProvider],
        "selected_ai_provider": effective_ai_provider.value,
        "backup_providers": [provider.value for provider in BackupProvider],
        "selected_backup_provider": effective_backup_provider.value,
        "backup_encryption_methods": [method.value for method in BackupEncryptionMethod],
        "selected_backup_encryption_method": effective_backup_encryption.value,
        "backup_api_key_env": backup_api_key_env,
        "has_saved_backup_password": has_saved_backup_password,
        "accounts": [_serialize_account(account) for account in (accounts or [])],
        "default_account_id": default_account_id,
        "anthropic_api_key": anthropic_api_key,
        "error_message": error_message,
    }
    return _render_ssr_or_fallback(sidecar=sidecar, route="create", props=props)


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
    launch_mode: LaunchMode = LaunchMode.DOCKER,
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
        # Drives the client-side time-based progress bar on the loading
        # screen (eases toward ~80% over this duration).
        expected_duration_seconds=expected_creation_duration_seconds(info.launch_mode),
    )


def render_welcome_page(sidecar: SsrSidecar | None = None) -> str:
    """Render the welcome/splash page for first-time users.

    The page itself is a Solid component (``routes/welcome.jsx``); this
    shim asks the sidecar to render it and falls back to the
    client-render shell when the sidecar isn't available.
    """
    return _render_ssr_or_fallback(sidecar=sidecar, route="welcome", props={})


def render_login_page(sidecar: SsrSidecar | None = None) -> str:
    """Render the login prompt page for unauthenticated users."""
    return _render_ssr_or_fallback(sidecar=sidecar, route="login", props={})


def render_login_redirect_page(
    one_time_code: OneTimeCode,
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the JS redirect page that forwards to /authenticate.

    The one-time code is passed verbatim as a Solid prop and encoded
    with ``encodeURIComponent`` at navigation time on the client. We
    never interpolate it into a string literal here, matching the
    safety contract of the original Jinja template.
    """
    return _render_ssr_or_fallback(
        sidecar=sidecar,
        route="login_redirect",
        props={"one_time_code": str(one_time_code)},
    )


def render_auth_error_page(message: str, sidecar: SsrSidecar | None = None) -> str:
    """Render an error page for failed authentication."""
    return _render_ssr_or_fallback(
        sidecar=sidecar,
        route="auth_error",
        props={"message": message},
    )


def render_request_unavailable_page(message: str) -> str:
    """Render the page shown when a request is already resolved or missing."""
    return JINJA_ENV.get_template("request_unavailable.html").render(message=message)


def render_recovery_page(
    agent_id: AgentId,
    return_to: str,
    initial_status: str,
    initial_error: str,
    ssh_command: str | None = None,
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the workspace-recovery page shown when the system interface is unresponsive.

    Built on the shared ``render_loading_page`` so the recovery page's loading
    state is identical to the mngr_forward proxy loader. ``initial_status`` is
    one of ``"stuck"``/``"restarting"``/``"restart_failed"``/``"healthy"`` and
    governs the page's initial UI state. ``initial_error`` is the failure
    reason shown (collapsed) when ``initial_status`` is ``"restart_failed"``.
    ``return_to`` is the URL the page navigates back to once the workspace is
    healthy again.

    ``ssh_command`` is the copy-pasteable SSH command for the agent's host. When
    provided, a "Copy SSH command" button sits beside "Copy diagnostics" in the
    Diagnostics menu; when ``None`` (no SSH info -- e.g. the brief window before
    discovery surfaces it) the button is omitted entirely rather than rendered
    inert.
    """
    props: dict[str, Any] = {
        "agent_id": str(agent_id),
        "return_to": return_to,
        "initial_status": initial_status,
        "initial_error": initial_error,
        "ssh_command": ssh_command,
        "accent": workspace_accent(str(agent_id)),
    }
    return _render_ssr_or_fallback(sidecar=sidecar, route="recovery", props=props)


def render_destroying_page(
    agent_id: AgentId,
    agent_name: str,
    pid: int,
    status: str,
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the detail page for an in-flight or recently-completed destroy.

    The page polls ``/api/destroying/<agent_id>/{status,log}`` to keep its
    log tail and status badge up to date; once status flips to ``done`` it
    redirects to ``/``. ``status`` is the initial server-side computed
    value (``running``/``failed``/``done``) so the page renders correctly
    even before the first poll completes.
    """
    props: dict[str, Any] = {
        "agent_id": str(agent_id),
        "agent_name": agent_name,
        "pid": int(pid),
        "status": status,
        "accent": workspace_accent(str(agent_id)),
    }
    return _render_ssr_or_fallback(sidecar=sidecar, route="destroying", props=props)


# -- Chrome (persistent shell) templates --


def render_chrome_page(
    is_mac: bool = False,
    is_authenticated: bool = False,
    mngr_forward_origin: str = "",
    initial_workspaces: Sequence[dict[str, str]] | None = None,
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the persistent chrome page (title bar + sidebar + content iframe).

    is_mac controls whether macOS-specific styling is applied (traffic light padding,
    hidden window controls).

    ``mngr_forward_origin`` is exposed to the page-level JS via a
    ``data-mngr-forward-origin`` attribute on the body so chrome.js can build
    workspace links that target the plugin's port directly.

    In Electron mode, the iframe and browser sidebar are hidden via JS; the content
    and sidebar are handled by separate WebContentsViews.

    The page is implemented as a Solid component (``routes/chrome.jsx``);
    this shim asks the chrome SSR bundle to render it and falls back to
    the client-render shell when the sidecar isn't available.
    """
    return _render_ssr_or_fallback(
        sidecar=sidecar,
        route="chrome",
        props={
            "isMac": bool(is_mac),
            "isAuthenticated": bool(is_authenticated),
            "mngrForwardOrigin": mngr_forward_origin,
            "initialWorkspaces": list(initial_workspaces or []),
        },
        bundle="chrome",
    )


def render_sidebar_page(
    mngr_forward_origin: str = "",
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the standalone sidebar page for the Electron sidebar WebContentsView.

    This page shows the workspace list and subscribes to SSE updates. In Electron,
    clicking a workspace sends an IPC message via the preload bridge to navigate
    the content WebContentsView. ``mngr_forward_origin`` is exposed via
    ``data-mngr-forward-origin`` so sidebar.js can build the cross-origin
    ``/goto/<agent>/`` URL the plugin serves.

    Implemented as a Solid component (``routes/sidebar.jsx``); the shim
    asks the sidebar SSR bundle to render it and falls back to the
    client-render shell when the sidecar isn't available.
    """
    return _render_ssr_or_fallback(
        sidecar=sidecar,
        route="sidebar",
        props={"mngrForwardOrigin": mngr_forward_origin},
        bundle="sidebar",
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


# -- Dev styleguide --


@pure
def render_dev_styleguide_page() -> str:
    """Render the styleguide page (mounted at ``/_dev/styleguide``).

    The page is a hand-authored catalog of UI patterns and tokens. When a
    new ``:root`` token is added to ``static/tokens.css``, add a swatch
    in ``templates/dev_styleguide.html`` with ``data-token="--<name>"``
    on its wrapper -- the ``templates_test.py`` ratchet cross-checks the
    set of declared ``:root`` tokens against the set of ``data-token``
    swatches and fails if either side drifts.
    """
    return JINJA_ENV.get_template("dev_styleguide.html").render()


def _serialize_account_for_solid(account: AccountSession) -> dict[str, Any]:
    """Flatten an ``AccountSession`` to the JSON-serializable shape the Solid accounts page expects.

    Mirrors the fields the original Jinja template read off each account
    (``email``, ``user_id``, ``workspace_ids``) plus nothing else. Used by
    ``render_accounts_page`` (and exposed for tests).
    """
    return {
        "user_id": str(account.user_id),
        "email": account.email,
        "workspace_ids": list(account.workspace_ids),
    }


def render_accounts_page(
    accounts: Sequence[AccountSession],
    default_account_id: str | None = None,
    enabled_by_user_id: Mapping[str, bool] | None = None,
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the manage accounts page.

    ``enabled_by_user_id`` maps each account's user_id to whether its
    ``[providers.imbue_cloud_<slug>]`` block is enabled in settings.toml.
    The page renders a "Signed out" indicator when an account is present
    (still in sessions.json) but the user disabled the block via the
    providers panel.
    """
    props: dict[str, Any] = {
        "accounts": [_serialize_account_for_solid(a) for a in accounts],
        "default_account_id": default_account_id or "",
        "enabled_by_user_id": dict(enabled_by_user_id or {}),
    }
    return _render_ssr_or_fallback(sidecar=sidecar, route="accounts", props=props)
