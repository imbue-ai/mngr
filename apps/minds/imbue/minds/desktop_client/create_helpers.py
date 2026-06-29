"""Shared helpers for the workspace create flow.

Used by both the browser create page (``app.py``) and the versioned
``POST /api/v1/workspaces`` route (``api_v1.py``) so the two create front
doors compute the same auto-name and color from the same logic. Lives in a
lower module (rather than in ``app.py``) so ``api_v1`` can reuse it without
importing the app module.
"""

from typing import Final

from loguru import logger

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_color import normalize_workspace_color

# Where the create flow sends a user who chose the remote (Imbue Cloud) preset
# without any signed-in account: into the sign-up/sign-in flow with a link back
# to the picker. ``return_to=%2Fcreate`` is the URL-encoded ``/create``. The
# create response carries this as its ``redirect_url`` and the create-page JS
# navigates there (the no-account backstop for the remote preset).
REMOTE_SIGNIN_REDIRECT_URL: Final[str] = "/auth/signup?return_to=%2Fcreate"


def existing_workspace_host_names(backend_resolver: BackendResolverInterface) -> set[str]:
    """Gather the host names of every known workspace across all providers.

    Reads the resolver's discovery snapshot (the aggregated view over all
    providers) rather than shelling out per workspace, per the resolver-cache
    read convention. Uses ``list_known_workspace_ids`` -- the *full* set,
    including workspaces on destroyed-but-still-lingering hosts -- so an
    auto-generated ``mind-N`` name does not collide with one that discovery has
    not yet fully dropped. Feeds both the duplicate-name guard and the
    ``mind-N`` auto-naming in ``resolve_create_host_name``.
    """
    names: set[str] = set()
    for aid in backend_resolver.list_known_workspace_ids():
        name = backend_resolver.get_workspace_name(aid)
        if name is not None:
            names.add(name)
    return names


def taken_host_names_on_provider(backend_resolver: BackendResolverInterface, provider_instance_name: str) -> set[str]:
    """Case-folded names of active workspaces on a single provider instance.

    Scopes to the provider instance a create would target -- where the host-name
    uniqueness check actually fires -- and to *active* workspaces only (a
    destroyed host's name is free to reuse), reading the discovery snapshot per
    the resolver-cache read convention. Names are case-folded so the create
    form's availability check treats ``My-Mind`` and ``my-mind`` as the same
    name. Feeds the ``GET /api/v1/desktop/host-name-available`` check.
    """
    taken: set[str] = set()
    for agent_id in backend_resolver.list_active_workspace_ids():
        info = backend_resolver.get_agent_display_info(agent_id)
        if info is None or info.provider_name != provider_instance_name:
            continue
        name = backend_resolver.get_workspace_name(agent_id)
        if name is not None:
            taken.add(name.casefold())
    return taken


def color_for_new_workspace(raw_color: object) -> str:
    """Lenient parse of a create request's submitted color, with default fallback.

    The create page posts a hidden ``color`` input and the JSON API accepts an
    optional ``color`` field. A missing or malformed value (e.g. the browser ate
    the input) must not reject the whole create request -- the new workspace just
    gets the default color. A *missing* color (an absent field, or an explicit
    JSON ``null``) is normal flow (the field is optional) and stays silent; a
    non-empty value that fails to parse indicates a buggy client, so it is logged
    before falling back.
    """
    stripped = str(raw_color).strip() if raw_color is not None else ""
    normalized = normalize_workspace_color(stripped)
    if normalized is not None:
        return normalized
    if stripped:
        logger.warning("Ignoring malformed create-request color {!r}; using the default workspace color.", stripped)
    return DEFAULT_WORKSPACE_COLOR
