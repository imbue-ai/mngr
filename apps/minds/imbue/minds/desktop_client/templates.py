"""HTML rendering for the desktop client.

Each ``render_*`` function is a thin wrapper around a JinjaX component
under ``templates/`` in this directory, rendered through the shared
``CATALOG``. Primitive components (Button, Card, Notice, Spinner,
TextInput, Opt, ...) and the page layout (``Base``) sit at the top of
``templates/``; full pages live under ``templates/pages/`` as PascalCase
``.jinja`` files; auth pages and the OAuth icon component live under
``templates/auth/``. Tests call these functions directly; the FastAPI
route handlers call them the same way. The public signatures are stable
so neither callers nor tests have to know the templates moved from raw
Jinja2 macros + ``{% extends %}`` to JinjaX components.
"""

import hashlib
import html
import os
import re
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from jinja2 import Environment
from jinja2 import select_autoescape
from jinjax import Catalog

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.agent_creator import AgentCreationInfo
from imbue.minds.desktop_client.onboarding import expected_creation_duration_seconds
from imbue.minds.primitives import AIProvider
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider
from imbue.minds.primitives import CreationId
from imbue.minds.primitives import LaunchMode
from imbue.minds.primitives import OneTimeCode
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.loading_page import render_loading_page

TEMPLATE_DIR: Final[Path] = Path(__file__).resolve().parent / "templates"

# Shared Tailwind class strings for the three button components
# (Button.jinja, ButtonLink.jinja, ButtonSubmit.jinja). Exposed as JinjaX
# Catalog globals so a single edit here updates every button variant; the
# alternative -- inlining the same class string in three sibling templates
# -- drifted across files trivially. Surface as uppercase to match the
# `CATALOG` constant convention and to mark them as Jinja globals (not
# per-render context).
#
# Size axis is independent of variant -- size dictates geometry (padding,
# radius, font weight, text size), variant dictates color. ``md`` is the
# default in-flow button; ``lg`` is the prominent block CTA used on the
# auth flow; ``icon`` is a square padding for icon-only buttons (e.g. the
# restart / settings icons in the Landing project row).
_BTN_BASE: Final[str] = (
    "inline-flex items-center justify-center gap-1.5 leading-tight "
    "transition-colors disabled:opacity-30 disabled:cursor-not-allowed "
    "cursor-pointer no-underline whitespace-nowrap"
)
_BTN_SIZES: Final[Mapping[str, str]] = {
    "md": "px-3.5 py-2 rounded-md font-medium text-sm",
    "lg": "px-4 py-3 rounded-lg font-semibold text-base",
    "icon": "p-1.5 rounded-md font-medium text-sm",
}
_BTN_VARIANTS: Final[Mapping[str, str]] = {
    "primary": "bg-zinc-900 text-zinc-50 border border-transparent hover:bg-zinc-800",
    "secondary": "bg-zinc-100 text-zinc-900 border border-zinc-200 hover:bg-zinc-200",
    "danger": "bg-red-50 text-red-600 border border-red-200 hover:bg-red-100",
    "success": "bg-emerald-800 text-emerald-50 border border-transparent hover:bg-emerald-900",
    "ghost": "bg-transparent text-zinc-700 border border-transparent hover:bg-zinc-100 hover:text-zinc-900",
}

# Shared Tailwind class string for the three form-control components
# (TextInput.jinja, Select.jinja, Textarea.jinja). Exposed as a Catalog
# global so the focus-ring token, border, padding and text size live in
# exactly one place. Width and border-radius vary per-component so they
# are NOT included here -- each component sets its own.
_INPUT_BASE: Final[str] = (
    "px-3 py-2.5 text-sm border border-zinc-200 bg-white text-zinc-900 "
    "outline-none transition focus:border-blue-600 focus:ring-2 focus:ring-blue-600/15"
)

# Inner SVG path data for the lucide-style 24x24 stroke icons. The
# Icon24.jinja component wraps these in the canonical stroke shell
# (fill=none, stroke=currentColor, stroke-width=2, stroke-linecap=round,
# stroke-linejoin=round). The dict is the single source of truth -- to
# add or swap an icon, edit one entry here.
_ICONS_24: Final[Mapping[str, str]] = {
    "sidebar": '<rect x="3" y="3" width="18" height="18" rx="2"/><line x1="9" y1="3" x2="9" y2="21"/>',
    "home": '<path d="M3 12L12 3l9 9"/><path d="M5 10v10a1 1 0 0 0 1 1h4v-6h4v6h4a1 1 0 0 0 1-1V10"/>',
    "back": '<polyline points="15 18 9 12 15 6"/>',
    "forward": '<polyline points="9 6 15 12 9 18"/>',
    "messages": '<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>',
    "restart": '<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>',
    "settings": (
        '<circle cx="12" cy="12" r="3"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06'
        "a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09"
        "A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83"
        "l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09"
        "A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83"
        "l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09"
        "a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83"
        "l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09"
        'a1.65 1.65 0 0 0-1.51 1z"/>'
    ),
    "external": (
        '<path d="M14 3h7v7"/><path d="M10 14L21 3"/>'
        '<path d="M21 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5"/>'
    ),
}

# 12x12 chrome glyph path data (minimize / maximize / close). Title-bar
# window controls only; rendered through Icon12.jinja, which wraps these
# in the same stroke shell as Icon24 but with the smaller viewBox + size.
_ICONS_12: Final[Mapping[str, str]] = {
    "minimize": '<line x1="2" y1="6" x2="10" y2="6"/>',
    "maximize": '<rect x="2" y="2" width="8" height="8" rx="0.5"/>',
    "close": '<line x1="2" y1="2" x2="10" y2="10"/><line x1="10" y1="2" x2="2" y2="10"/>',
}


def _build_catalog() -> Catalog:
    """Build the JinjaX Catalog used to render every desktop-client template.

    JinjaX builds its own internal Jinja Environment but copies autoescape +
    filters from any seed env you pass in. We seed with the same autoescape
    config the old standalone JINJA_ENV used so user-controlled strings (form
    errors, agent IDs, etc.) stay HTML-escaped exactly as before.

    ``BTN_BASE`` / ``BTN_VARIANTS`` are exposed as Jinja globals so the
    three button components can share a single source of truth instead of
    each redeclaring the same class string + variants map.
    """
    seed_env = Environment(
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    catalog = Catalog(
        jinja_env=seed_env,
        globals={
            "BTN_BASE": _BTN_BASE,
            "BTN_SIZES": _BTN_SIZES,
            "BTN_VARIANTS": _BTN_VARIANTS,
            "INPUT_BASE": _INPUT_BASE,
            "ICONS_24": _ICONS_24,
            "ICONS_12": _ICONS_12,
        },
    )
    catalog.add_folder(str(TEMPLATE_DIR))
    return catalog


CATALOG: Final[Catalog] = _build_catalog()


# -- Per-workspace identity color --
# See docs on workspace_accent() for why OKLCH + fixed L/C + SHA-256-derived
# hue. Mirrored on the JS side in static/workspace_accent.js (the shared
# window.mindsAccent helper consumed by chrome.js and sidebar.js).

# Lightness percent and chroma for the OKLCH workspace accent. Fixed across
# all workspaces so the only axis of variation is the hue. The accent fills
# the full-width titlebar (not just a small swatch), so a light /
# low-saturation tone is needed to read as chrome rather than a saturated
# highlight.
_WORKSPACE_L: Final[int] = 85
_WORKSPACE_C: Final[float] = 0.08


@pure
def workspace_accent(agent_id: str) -> str:
    """Deterministically map an agent id to a CSS OKLCH color.

    Uses a fixed lightness and chroma (a light, low-saturation tone that
    reads as a chrome surface across the full-width titlebar) so every
    workspace accent sits at the same readable level, and only the hue
    varies. Full 360 degree hue range means collisions are effectively
    impossible, and OKLCH's perceptual uniformity means close hashes
    still read as visibly different colors.
    """
    digest = hashlib.sha256(agent_id.encode("utf-8")).digest()
    hue = int.from_bytes(digest[:4], "big") % 360
    return f"oklch({_WORKSPACE_L}% {_WORKSPACE_C} {hue})"


# -- Workspace palette --
#
# Twelve user-pickable workspace colors. Eleven named entries come from
# the Figma source (Minds Early IA Explorations, node 356:4113); the
# twelfth ("white") is added so users have a neutral light option
# distinct from the warm-cream Figma entries. Names are kebab-case and
# are not surfaced in the UI today (the picker shows unlabeled
# swatches); they exist so the system can refer to the default by
# name in code and so the same name list is auditable in both Python
# and JS. Mirrored 1:1 in static/workspace_accent.js -- a drift guard
# in templates_test.py parses the JS file and asserts equality.

WORKSPACE_PALETTE: Final[Mapping[str, str]] = {
    "indifference": "#000000",
    "confusion": "#0b292b",
    "courage": "#492222",
    "envy": "#3c3d06",
    "peace": "#9fbbd3",
    "belonging": "#e8a7a8",
    "energy": "#cecd0c",
    "strength": "#cfc7b3",
    "comfort": "#f5d6a0",
    "inspiration": "#e9ecd9",
    "clarity": "#fcefd4",
    "white": "#ffffff",
}

# Default workspace color used at create time and for the one-time
# migration backfill applied to any primary agent that lacks a
# ``color`` label after the upgrade.
DEFAULT_WORKSPACE_COLOR_NAME: Final[str] = "confusion"
DEFAULT_WORKSPACE_COLOR: Final[str] = WORKSPACE_PALETTE[DEFAULT_WORKSPACE_COLOR_NAME]

# WCAG relative luminance threshold below which white text reads
# better than black against the background. The exact crossover is
# sqrt(1.05 * 0.05) - 0.05 ~= 0.1791; we use the rounded 0.179
# directly. The standard 0.03928 / 12.92 / 1.055 / 2.4 sRGB linearization
# numbers come from the WCAG 2.x relative-luminance definition.
_FOREGROUND_LUMINANCE_THRESHOLD: Final[float] = 0.179

_HEX_PATTERN: Final[re.Pattern[str]] = re.compile(r"^#?([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


@pure
def normalize_workspace_color(value: str) -> str | None:
    """Lenient hex parser for workspace color inputs.

    Accepts ``#fff`` / ``fff`` / ``#ffffff`` / ``ffffff`` in any case
    (with leading/trailing whitespace tolerated). Returns the canonical
    ``#rrggbb`` lowercase form on success, or ``None`` if the input is
    not a recognized 3- or 6-character hex literal. Alpha channel
    inputs (``#rrggbbaa``) are rejected; the picker UI does not offer
    them and they would propagate as invisible chrome.
    """
    match = _HEX_PATTERN.match(value.strip())
    if not match:
        return None
    body = match.group(1).lower()
    if len(body) == 3:
        body = "".join(ch * 2 for ch in body)
    return f"#{body}"


@pure
def pick_workspace_foreground(hex_color: str) -> str:
    """Return the contrasting RGB triple for titlebar text/icons over ``hex_color``.

    The returned value is ``"0 0 0"`` (black) or ``"255 255 255"``
    (white), suitable for dropping into ``rgb(var(--titlebar-fg) / <alpha>)``.
    Chooses by WCAG relative luminance so the picker stays legible across
    the whole 12-color palette and any custom hex -- replacing the prior
    fixed-OKLCH-L-85 picker that always emitted black.

    ``hex_color`` must be a normalized ``#rrggbb`` (lowercase). Callers
    should pass values through ``normalize_workspace_color`` first.
    """
    r = int(hex_color[1:3], 16) / 255.0
    g = int(hex_color[3:5], 16) / 255.0
    b = int(hex_color[5:7], 16) / 255.0

    def _linearize(channel: float) -> float:
        if channel <= 0.03928:
            return channel / 12.92
        return ((channel + 0.055) / 1.055) ** 2.4

    luminance = (
        0.2126 * _linearize(r)
        + 0.7152 * _linearize(g)
        + 0.0722 * _linearize(b)
    )
    return "0 0 0" if luminance > _FOREGROUND_LUMINANCE_THRESHOLD else "255 255 255"


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
    return CATALOG.render(
        "pages.Landing",
        agent_ids=accessible_agent_ids,
        agent_accents=agent_accents,
        mngr_forward_origin=mngr_forward_origin,
        telegram_enabled=telegram_status_by_agent_id is not None,
        telegram_status_by_agent_id=telegram_status_by_agent_id or {},
        is_discovering=is_discovering,
        agent_names=agent_names or {},
        destroying_status_by_agent_id=destroying_status_by_agent_id or {},
    )


# Hardcoded fallbacks for the workspace-creation form. Overridable via the
# MINDS_WORKSPACE_* env vars only when the operator explicitly opts in -- see
# ``_operator_workspace_default`` for the gating rationale.
_FALLBACK_GIT_URL: Final[str] = "https://github.com/imbue-ai/forever-claude-template.git"
_FALLBACK_HOST_NAME: Final[str] = "assistant"
_FALLBACK_BRANCH: Final[str] = ""

# Env var (set by ``just minds-start`` and the e2e workspace runner) that opts a
# launch into the operator's local-worktree create-form defaults. Gating on an
# explicit opt-in -- rather than on the tier -- means dev iteration works on ANY
# tier (including staging / production) when launched via ``just minds-start``,
# while a normal end-user ``minds run`` never honors a stray MINDS_WORKSPACE_*
# left over in the operator's shell, on any tier. The previous tier-based gate
# did the opposite: it blocked legitimate dev iteration on staging (forcing the
# form back to the public GitHub FCT on ``main``) while leaving dev tiers exposed
# to stray vars.
_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR: Final[str] = "MINDS_USE_LOCAL_WORKSPACE_DEFAULTS"


def _operator_workspace_default(env_var: str, fallback: str) -> str:
    """Return ``env_var`` only when the operator explicitly opted in; else ``fallback``.

    The MINDS_WORKSPACE_GIT_URL / _NAME / _BRANCH env vars wire the create-form
    defaults to the operator's local FCT worktree. They are honored only when
    ``MINDS_USE_LOCAL_WORKSPACE_DEFAULTS=1`` is set in the same environment
    (``just minds-start`` and the e2e runner set it). An end-user ``minds run``
    never sets it, so a stray MINDS_WORKSPACE_* left in the shell is ignored on
    every tier -- the safety the previous tier-based gate provided, without also
    blocking dev iteration on staging / production.

    These defaults point at a *local* path and a dev branch, which only make
    sense for local-compute launch modes (Lima / Docker). For IMBUE_CLOUD (pool
    lease) they must not be kept -- a pool host cannot clone a local path and the
    dev branch matches no pre-baked host -- so the opt-in is the operator's
    signal that they are doing local dev iteration, not an end-user pool create.
    """
    if os.environ.get(_WORKSPACE_DEFAULTS_OPT_IN_ENV_VAR) != "1":
        return fallback
    return os.environ.get(env_var, fallback)


@pure
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
    accounts: Sequence[object] | None = None,
    default_account_id: str = "",
    anthropic_api_key: str = "",
    error_message: str = "",
    region_options_by_launch_mode: Mapping[str, Sequence[str]] | None = None,
    region_selected_by_launch_mode: Mapping[str, str] | None = None,
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
    effective_url = git_url if git_url else _operator_workspace_default("MINDS_WORKSPACE_GIT_URL", _FALLBACK_GIT_URL)
    effective_name = (
        host_name if host_name else _operator_workspace_default("MINDS_WORKSPACE_NAME", _FALLBACK_HOST_NAME)
    )
    effective_branch = branch if branch else _operator_workspace_default("MINDS_WORKSPACE_BRANCH", _FALLBACK_BRANCH)
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
    return CATALOG.render(
        "pages.Create",
        git_url=effective_url,
        host_name=effective_name,
        branch=effective_branch,
        launch_modes=list(LaunchMode),
        selected_launch_mode=effective_launch_mode.value,
        ai_providers=list(AIProvider),
        selected_ai_provider=effective_ai_provider.value,
        backup_providers=list(BackupProvider),
        selected_backup_provider=effective_backup_provider.value,
        backup_encryption_methods=list(BackupEncryptionMethod),
        selected_backup_encryption_method=effective_backup_encryption.value,
        backup_api_key_env=backup_api_key_env,
        has_saved_backup_password=has_saved_backup_password,
        accounts=accounts or [],
        default_account_id=default_account_id,
        anthropic_api_key=anthropic_api_key,
        error_message=error_message,
        region_options_by_launch_mode={
            key: list(value) for key, value in (region_options_by_launch_mode or {}).items()
        },
        region_selected_by_launch_mode=dict(region_selected_by_launch_mode or {}),
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
    return CATALOG.render(
        "pages.Creating",
        agent_id=creation_id,
        status_text=status_text,
        # Drives the client-side time-based progress bar on the loading
        # screen (eases toward ~80% over this duration).
        expected_duration_seconds=expected_creation_duration_seconds(info.launch_mode),
    )


@pure
def render_welcome_page() -> str:
    """Render the welcome/splash page for first-time users."""
    return CATALOG.render("pages.Welcome")


@pure
def render_login_page() -> str:
    """Render the login prompt page for unauthenticated users."""
    return CATALOG.render("pages.Login")


@pure
def render_login_redirect_page(one_time_code: OneTimeCode) -> str:
    """Render the JS redirect page that forwards to /authenticate."""
    return CATALOG.render("pages.LoginRedirect", one_time_code=one_time_code)


@pure
def render_auth_error_page(message: str) -> str:
    """Render an error page for failed authentication."""
    return CATALOG.render("pages.AuthError", message=message)


@pure
def render_inbox_page(
    cards: Sequence[Mapping[str, str]],
    selected_id: str = "",
    detail_html: str = "",
    is_empty: bool = False,
    auto_open: bool = True,
) -> str:
    """Render the full inbox modal page served by ``GET /inbox``.

    ``cards`` is the initial left-list content (most-recent-first).
    ``selected_id`` highlights one card; ``detail_html`` is the
    pre-rendered right-pane fragment (handler detail, unavailable
    fragment, or empty). ``is_empty`` is True when there are no
    pending requests and the layout collapses to a centered message.
    ``auto_open`` is the initial state of the "Auto-open on new
    request" checkbox in the inbox header.
    """
    return CATALOG.render(
        "pages.Inbox",
        cards=cards,
        selected_id=selected_id,
        detail_html=detail_html,
        is_empty=is_empty,
        auto_open=auto_open,
    )


@pure
def render_inbox_list_fragment(
    cards: Sequence[Mapping[str, str]],
    selected_id: str = "",
) -> str:
    """Render the inbox left-list fragment served by ``GET /inbox/list``."""
    return CATALOG.render("InboxList", cards=cards, selected_id=selected_id)


@pure
def render_inbox_unavailable_fragment(message: str = "") -> str:
    """Render the inbox right-pane "no longer available" fragment.

    Returned by ``GET /inbox/detail/<id>`` when the id is unknown or
    already resolved; also innerHTML-swapped into the right pane by the
    inbox shell JS when an SSE event resolves the currently-selected
    item.

    ``message`` is an optional supporting sentence rendered under the
    fragment's heading. When empty (the default), only the heading is
    shown, so callers that drop the supporting sentence don't end up
    duplicating the heading.
    """
    return CATALOG.render("InboxUnavailable", message=message)


# CSS for the recovery page's restart controls, appended to the shared
# ``LOADING_PAGE_CSS``. The card itself, spinner, heading and message all come
# from the shared loading page, so the recovery page's loading state is
# byte-identical to the mngr_forward proxy loader.
_RECOVERY_STYLE: Final[str] = """\
      .hidden { display: none; }

      /* Keep the whole card within the viewport and lay it out as a vertical
         stack: the header row and the restart button stay pinned at the top,
         and only the troubleshooting block scrolls when its disclosures are
         expanded. Without this the card grows past the viewport as dropdowns
         open and -- because the body flex-centers it -- the heading and button
         slide off the top, out of reach of the page scrollbar. This overrides
         the shared ``.card`` from LOADING_PAGE_CSS (appended after it, so it
         wins); the proxy loader never pulls in this style, so it is unaffected.
         The 48px subtracted matches the body's 24px top+bottom padding. */
      .card {
        display: flex;
        flex-direction: column;
        max-height: calc(100vh - 48px);
      }
      .row { flex-shrink: 0; }

      /* Primary action. The restart button is the page's focal point: full
         width, prominent, directly under the message. Most users only ever
         need this -- the troubleshooting disclosures below are for the rare
         deep-debugging case. */
      #recovery-host-btn {
        margin-top: 20px;
        flex-shrink: 0;
        width: 100%;
        background: #18181b;
        color: #fff;
        border: 0;
        border-radius: 8px;
        padding: 12px 16px;
        font-size: 0.9375rem;
        font-weight: 600;
        cursor: pointer;
      }
      #recovery-host-btn:hover { background: #3f3f46; }
      #recovery-host-btn.secondary { background: #6b7280; }
      #recovery-host-btn.secondary:hover { background: #4b5563; }

      /* Secondary, rarely-needed troubleshooting block: the error and
         diagnostics disclosures, grouped below a muted label and a thin
         divider. The whole block self-hides whenever neither disclosure is
         currently shown (both carry ``.hidden``), so the divider and label
         never appear over an empty section. */
      .recovery-troubleshooting {
        margin-top: 20px;
        padding-top: 16px;
        border-top: 1px solid #f4f4f5;
        /* The block can shrink below its content height (min-height: 0 frees
           it from the default flex min-content floor) and scrolls internally
           once the card hits its viewport cap, so expanding many disclosures
           never pushes the pinned header and button off-screen. */
        min-height: 0;
        overflow-y: auto;
      }
      .recovery-troubleshooting:not(:has(> details:not(.hidden))) { display: none; }
      .recovery-troubleshooting-label {
        font-size: 0.6875rem;
        font-weight: 600;
        letter-spacing: 0.04em;
        text-transform: uppercase;
        color: #a1a1aa;
        margin: 0 0 6px;
      }
      .recovery-troubleshooting > details {
        margin: 0 0 8px;
        border: 1px solid #f4f4f5;
        background: #fff;
        border-radius: 8px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
        color: #52525b;
      }
      .recovery-troubleshooting > details:last-child { margin-bottom: 0; }
      .recovery-troubleshooting > details > summary {
        display: flex;
        align-items: center;
        justify-content: space-between;
        cursor: pointer;
        padding: 9px 12px;
        font-weight: 500;
        font-size: 0.8125rem;
        color: #52525b;
        list-style: none;
      }
      .recovery-troubleshooting > details > summary::-webkit-details-marker { display: none; }
      .recovery-troubleshooting > details > summary::after {
        content: "\\25BE";
        color: #a1a1aa;
        font-size: 0.75rem;
        transition: transform 0.15s;
      }
      .recovery-troubleshooting > details[open] > summary::after { transform: rotate(180deg); }
      .recovery-troubleshooting > details > summary:hover { color: #3f3f46; }
      .recovery-troubleshooting > details[open] > summary { border-bottom: 1px solid #f4f4f5; }
      .recovery-troubleshooting > details > :not(summary) { padding: 10px 12px; }

      details pre {
        margin: 0;
        padding: 10px 12px;
        max-height: 240px;
        overflow-y: auto;
        white-space: pre-wrap;
        overflow-wrap: anywhere;
        font-size: 0.75rem;
        line-height: 1.5;
        font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
        background: #fafafa;
        color: #3f3f46;
        border-radius: 6px;
      }
      .probe-row {
        margin: 4px 0 0;
        border: 1px solid #f4f4f5;
        background: #fff;
        border-radius: 6px;
        box-shadow: 0 1px 2px rgba(0, 0, 0, 0.04);
      }
      .probe-row summary {
        display: flex;
        align-items: center;
        gap: 8px;
        padding: 6px 10px;
        font-size: 0.8125rem;
        font-weight: 500;
        cursor: pointer;
        color: #52525b;
        list-style: none;
      }
      .probe-row summary::-webkit-details-marker { display: none; }
      .probe-row summary::after {
        content: "\\25BE";
        color: #a1a1aa;
        font-size: 0.75rem;
        transition: transform 0.15s;
      }
      .probe-row[open] summary::after { transform: rotate(180deg); }
      .probe-row .probe-question { flex: 1; }
      .probe-glyph {
        display: inline-block;
        width: 1em;
        text-align: center;
        font-weight: 700;
      }
      .probe-glyph-yes { color: #047857; }
      .probe-glyph-no { color: #b91c1c; }
      .probe-glyph-unknown { color: #92400e; }
      #copy-diagnostics-btn,
      #copy-ssh-btn {
        margin-top: 8px;
        background: #fff;
        color: #52525b;
        border: 1px solid #d4d4d8;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 500;
        padding: 6px 12px;
        cursor: pointer;
      }
      #copy-ssh-btn { margin-left: 8px; }
      #copy-diagnostics-btn:hover,
      #copy-ssh-btn:hover { background: #f4f4f5; }
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
        var debugDetailsEl = document.getElementById('recovery-debug-details');
        var debugContentEl = document.getElementById('recovery-debug-content');
        var copyBtn = document.getElementById('copy-diagnostics-btn');
        // Present only for SSH-reachable hosts (every real workspace). Carries
        // the prebuilt connection command in its data attribute; absent (and so
        // null here) when the resolver has no SSH info for the agent.
        var copySshBtn = document.getElementById('copy-ssh-btn');

        var latestHealth = null;

        // A timed reload restarts the spinner's CSS animation from 0deg, so the
        // interval must be a whole multiple of the spinner's 1s rotation period
        // (see LOADING_PAGE_CSS' ``spin`` keyframe) -- otherwise the spinner
        // visibly jumps back mid-rotation on every refresh. 1000ms also matches
        // the mngr_forward proxy loader's 1s meta refresh, keeping the two
        // loading pages a user may see during recovery in lockstep.
        var REFRESH_INTERVAL_MS = 1000;

        function show(el, visible) {
          if (el) el.classList.toggle('hidden', !visible);
        }

        function escapeHtml(s) {
          if (s === null || s === undefined) return '';
          return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
        }

        function answerGlyph(answer) {
          if (answer === 'yes') return '<span class="probe-glyph probe-glyph-yes" aria-label="yes">&#x2713;</span>';
          if (answer === 'no') return '<span class="probe-glyph probe-glyph-no" aria-label="no">&#x2717;</span>';
          return '<span class="probe-glyph probe-glyph-unknown" aria-label="unknown">?</span>';
        }

        function renderDebugMenu(data) {
          if (!debugContentEl || !debugDetailsEl) return;
          if (!data || !Array.isArray(data.probes) || data.probes.length === 0) {
            debugContentEl.innerHTML = '';
            show(debugDetailsEl, false);
            return;
          }
          // Each probe is one row: glyph + question, with an expander
          // revealing the command that produced the answer and its raw output.
          var rows = data.probes.map(function (probe) {
            var glyph = answerGlyph(probe.answer);
            var body = '$ ' + probe.command + '\\n\\n' + probe.output;
            return '<details class="probe-row probe-row-' + escapeHtml(probe.answer || 'unknown') + '">'
              + '<summary>' + glyph + '<span class="probe-question">'
              + escapeHtml(probe.question) + '</span></summary>'
              + '<pre>' + escapeHtml(body) + '</pre>'
              + '</details>';
          });
          debugContentEl.innerHTML = rows.join('');
          show(debugDetailsEl, true);
        }

        function copyDiagnostics() {
          if (!latestHealth) return;
          try {
            var text = JSON.stringify(latestHealth, null, 2);
            if (navigator.clipboard) navigator.clipboard.writeText(text);
          } catch (e) {
            /* ignore */
          }
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
        // Background convergence poll for the restart_failed state. Unlike
        // scheduleRefresh (which reloads the whole page), this fetches pollUrl
        // with manual redirect handling: while the workspace is still down the
        // server returns the recovery HTML (200), which we discard so the
        // displayed failure reason + diagnostics stay put and the heavy
        // host-health probe is not re-run. Once the background probe loop flips
        // the tracker to HEALTHY the server starts 302ing to return_to, which
        // surfaces as an opaque-redirect response; we then follow it to send
        // the user back to the now-recovered workspace.
        function scheduleHealthyPoll() {
          setTimeout(function () {
            fetch(pollUrl(), { credentials: 'same-origin', redirect: 'manual' }).then(function (resp) {
              if (resp.type === 'opaqueredirect' || (resp.status >= 300 && resp.status < 400)) {
                window.location.assign(pollUrl());
                return;
              }
              scheduleHealthyPoll();
            }, function () {
              scheduleHealthyPoll();
            });
          }, REFRESH_INTERVAL_MS);
        }

        function renderLoading() {
          titleEl.textContent = 'Loading workspace';
          messageEl.textContent = '';
          show(spinnerEl, true);
          show(errorEl, false);
          show(hostBtn, false);
          // A stale diagnostic from the previous tick would be misleading
          // while we're in flight to a fresh check; hide it and drop the
          // cached payload so renderDebugMenu starts blank next time.
          show(debugDetailsEl, false);
          if (debugContentEl) debugContentEl.innerHTML = '';
          latestHealth = null;
        }
        // The shared "Workspace unresponsive" state -- shown for ambiguous-host
        // states, after a restart failure, and whenever the container is live
        // but unreachable (bouncing it would interrupt user agents, so we want
        // explicit consent before doing so).
        function renderUnresponsive() {
          titleEl.textContent = 'Workspace unresponsive';
          messageEl.textContent =
            'This workspace needs a restart to recover. In-progress work in all agents will be '
            + 'interrupted. If the problem persists, contact support.';
          show(spinnerEl, false);
          show(errorEl, true);
          hostBtn.textContent = 'Restart workspace';
          hostBtn.classList.remove('secondary');
          show(hostBtn, true);
        }
        // New tier: services.toml is missing [services.system_interface]. A
        // restart cannot recover this; the user has to fix the file. Provide
        // a secondary "Try restart anyway" affordance for completeness.
        function renderMisconfigured() {
          titleEl.textContent = 'Workspace misconfigured';
          messageEl.textContent =
            "This workspace's services.toml is missing the [services.system_interface] entry, "
            + 'so the system interface cannot be started. A restart is unlikely to help -- '
            + 'fix services.toml first. See the diagnostics below for details.';
          show(spinnerEl, false);
          show(errorEl, false);
          hostBtn.textContent = 'Try restart anyway';
          hostBtn.classList.add('secondary');
          show(hostBtn, true);
        }
        function renderDispatchError() {
          titleEl.textContent = 'Workspace unresponsive';
          messageEl.textContent = 'Could not start the restart. Check your connection and try again.';
          show(spinnerEl, false);
          show(errorEl, false);
          hostBtn.textContent = 'Restart workspace';
          hostBtn.classList.remove('secondary');
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

        // Fetch the host-health probe and populate the diagnostic. When
        // ``autoDispatch`` is true (the live stuck/probe entry) we also pick
        // a restart tier from ``dispatch_tier``; when it's false (the
        // restart_failed entry) we only render the diagnostic alongside the
        // existing failure-reason error block, so the user sees both.
        function runProbe(autoDispatch) {
          renderLoading();
          fetch('/api/agents/' + encodeURIComponent(agentId) + '/host-health', {
            credentials: 'same-origin',
          }).then(function (resp) {
            return resp.json();
          }).then(function (data) {
            latestHealth = data || null;
            renderDebugMenu(latestHealth);
            var tier = data && data.dispatch_tier;
            // A missing [services.system_interface] block means no restart can
            // recover the workspace, so honor this tier on every entry path --
            // including restart_failed, which is exactly the state a
            // misconfigured workspace lands in once its undeclared interface
            // fails to come back up. This must precede the no-auto-dispatch
            // short-circuit below; renderMisconfigured() dispatches nothing (it
            // only renders, with a "Try restart anyway" affordance), so it is
            // safe regardless of autoDispatch.
            if (tier === 'workspace_misconfigured') {
              renderMisconfigured();
              return;
            }
            if (!autoDispatch) {
              // restart_failed entry: render unresponsive so the failure
              // reason and the diagnostics list both stay visible.
              renderUnresponsive();
              return;
            }
            if (tier === 'host_offline') {
              // Container fully stopped: nothing live to interrupt, dispatch
              // unattended. Tell the endpoint the host is already stopped so it
              // skips the redundant stop step and cold-boots straight away.
              postRestart('/restart-host?host_already_stopped=1');
              return;
            }
            if (tier === 'interface_unresponsive') {
              // Container running, exec works: restart the system-services agent in place.
              postRestart('/restart-system-interface');
              return;
            }
            // 'host_unresponsive' or anything else: require explicit user consent for a host restart.
            renderUnresponsive();
          }, function () {
            renderUnresponsive();
          });
        }

        hostBtn.addEventListener('click', function () {
          postRestart('/restart-host');
        });
        if (copyBtn) {
          copyBtn.addEventListener('click', copyDiagnostics);
        }
        if (copySshBtn) {
          copySshBtn.addEventListener('click', function () {
            var cmd = copySshBtn.getAttribute('data-ssh-command') || '';
            try {
              if (navigator.clipboard) navigator.clipboard.writeText(cmd);
            } catch (e) {
              /* ignore */
            }
          });
        }

        if (initialStatus === 'restarting') {
          renderLoading();
          scheduleRefresh();
        } else if (initialStatus === 'restart_failed') {
          // Show the failure reason AND the diagnostic together: re-run
          // the probe with auto-dispatch off so the renderUnresponsive path
          // also has the diagnostics populated.
          runProbe(false);
          // A failed restart is not necessarily terminal: the background probe
          // loop keeps polling the workspace and may recover it on its own
          // (e.g. a cold container boot that finished just after the restart
          // worker's bounded wait elapsed). Watch for that recovery so we can
          // return the user to the workspace without them having to act.
          scheduleHealthyPoll();
        } else if (initialStatus === 'healthy') {
          // Degenerate: rendered HEALTHY with no return_to to 302 to. Offer a
          // manual restart rather than auto-dispatching one on a healthy page.
          renderUnresponsive();
        } else {
          runProbe(true);
        }
      })();
"""


@pure
def render_recovery_page(
    agent_id: AgentId,
    return_to: str,
    initial_status: str,
    initial_error: str,
    ssh_command: str | None = None,
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
    error_block = ""
    if initial_error:
        error_block = (
            '        <details id="recovery-error" class="hidden">\n'
            "          <summary>Error details</summary>\n"
            f"          <pre>{html.escape(initial_error)}</pre>\n"
            "        </details>\n"
        )
    # Debug details are populated dynamically by the recovery JS once it gets
    # a host-health response. The block is in the DOM from the start (hidden)
    # so the JS can fill it in place without re-templating.
    ssh_button = ""
    if ssh_command is not None:
        ssh_button = (
            '<button type="button" id="copy-ssh-btn" '
            f'data-ssh-command="{html.escape(ssh_command, quote=True)}">Copy SSH command</button>'
        )
    debug_block = (
        '        <details id="recovery-debug-details" class="hidden">\n'
        "          <summary>Diagnostics</summary>\n"
        '          <div id="recovery-debug-content"></div>\n'
        '          <div class="debug-section">'
        '<button type="button" id="copy-diagnostics-btn">Copy diagnostics</button>'
        f"{ssh_button}"
        "</div>\n"
        "        </details>\n"
    )
    # The restart button is the page's primary action, so it comes first --
    # directly under the message. The error and diagnostics disclosures are
    # grouped together below it in the de-emphasized troubleshooting block;
    # ``_RECOVERY_STYLE`` self-hides that block (divider + label included)
    # whenever neither disclosure is currently visible.
    card_extra = (
        '      <button id="recovery-host-btn" class="hidden">Restart workspace</button>\n'
        '      <div class="recovery-troubleshooting">\n'
        '        <p class="recovery-troubleshooting-label">Troubleshooting</p>\n'
        + error_block
        + debug_block
        + "      </div>\n"
    )
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
    return CATALOG.render(
        "pages.Destroying",
        agent_id=str(agent_id),
        agent_name=agent_name,
        pid=pid,
        status=status,
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
    return CATALOG.render(
        "pages.Chrome",
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
    return CATALOG.render(
        "pages.Sidebar",
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
    return CATALOG.render(
        "pages.Sharing",
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
    )


@pure
def render_workspace_settings(
    agent_id: str,
    ws_name: str,
    current_account: object | None,
    accounts: Sequence[object],
    servers: Sequence[str],
    telegram_state: str | None = None,
    is_leased_imbue_cloud: bool = False,
) -> str:
    """Render the workspace settings page.

    telegram_state controls whether the Telegram section is shown:

    - ``None`` -- no Telegram orchestrator configured; section is hidden.
    - ``"active"`` -- Telegram is already set up for this workspace.
    - ``"pending"`` -- setup button is shown.

    ``is_leased_imbue_cloud`` is True for workspaces on a host leased from
    Imbue Cloud; the account section then shows the bound account with a
    disabled Disassociate control and no association controls.

    Interactivity for the setup flow lives in ``static/workspace_settings.js``,
    which reads the agent id from the page's ``data-agent-id`` attribute.
    """
    return CATALOG.render(
        "pages.WorkspaceSettings",
        agent_id=agent_id,
        ws_name=ws_name,
        current_account=current_account,
        accounts=accounts,
        servers=servers,
        telegram_state=telegram_state,
        is_leased_imbue_cloud=is_leased_imbue_cloud,
    )


# -- Dev styleguide --


@pure
def render_dev_styleguide_page() -> str:
    """Render the styleguide page (mounted at ``/_dev/styleguide``).

    The page is a hand-authored catalog of UI patterns and tokens. When a
    new ``:root`` token is added to ``static/tokens.css``, add a swatch
    in ``templates/pages/DevStyleguide.jinja`` with
    ``data-token="--<name>"`` on its wrapper -- the ``templates_test.py``
    ratchet cross-checks the set of declared ``:root`` tokens against the
    set of ``data-token`` swatches and fails if either side drifts.
    """
    return CATALOG.render("pages.DevStyleguide")


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
    present (still in sessions.json) but the user disabled the block
    via the providers panel.
    """
    return CATALOG.render(
        "pages.Accounts",
        accounts=accounts,
        default_account_id=default_account_id or "",
        enabled_by_user_id=dict(enabled_by_user_id or {}),
    )
