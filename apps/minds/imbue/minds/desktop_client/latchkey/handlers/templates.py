"""Latchkey permission-detail HTML rendering.

The detail fragment renderers compose the shared Permissions* JinjaX
components (PermissionsHeader, PermissionsForm,
PermissionsManualCredentials, PermissionsError) into the right-pane
body for a single pending latchkey permission request. The inbox shell
provides the surrounding modal chrome (backdrop, close button, submit
JS, escape/backdrop dismiss).

* :func:`render_predefined_permission_dialog` renders the
  ``pages.LatchkeyPredefinedPermission`` component (checkbox per detent
  permission schema, with the auth-browser progress notice);
* :func:`render_file_sharing_permission_dialog` renders the
  ``pages.LatchkeyFileSharingPermission`` component (single hidden
  ``permissions=file-sharing`` input so the fragment reads as a plain
  yes/no for the requested path).

Keeping these renderers next to the handlers (rather than in the shared
``desktop_client/templates.py``) keeps the latchkey-shaped function
signatures -- notably the one that takes ``ServicePermissionInfo`` --
out of the generic template module.
"""

from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.templates import CATALOG
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME

# The catch-all ``any`` permission is stored and submitted verbatim (it is
# Detent's wildcard schema), but users find ``all`` clearer, so the dialog
# shows this label in its place while the underlying checkbox value stays
# ``any``.
_WILDCARD_PERMISSION_LABEL = "all"


@pure
def render_predefined_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    service: ServicePermissionInfo,
    checked_permissions: Sequence[str],
    will_open_browser: bool,
    mngr_forward_origin: str = "",
) -> str:
    """Render the predefined (catalog-backed) permission detail fragment.

    ``will_open_browser`` controls the in-progress notice shown after the
    user clicks Approve: when True (latchkey will run ``auth browser``),
    the notice tells the user to expect a browser pop-up; when False
    (credentials are already valid, or the service requires manual
    credentials), it shows a generic ``Granting permission...`` message.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the fragment points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return CATALOG.render(
        "pages.LatchkeyPredefinedPermission",
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        display_name=service.display_name,
        scope=service.scope,
        permission_schemas=service.permission_schemas,
        description_by_permission_name=service.description_by_permission_name,
        checked_permissions=set(checked_permissions),
        wildcard_permission=WILDCARD_PERMISSION_NAME,
        wildcard_label=_WILDCARD_PERMISSION_LABEL,
        will_open_browser=will_open_browser,
        mngr_forward_origin=mngr_forward_origin,
    )


@pure
def render_file_sharing_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    file_path: str,
    access: str,
    access_human_label: str,
    allowed_roots_json: str = "[]",
    home_dir: str = "",
    mngr_forward_origin: str = "",
) -> str:
    """Render the file-sharing permission detail fragment.

    Mirrors the predefined dialog's header, rationale card, and
    submission form (via the shared Permissions* JinjaX components);
    swaps the per-permission checkbox list for a short explanation of
    what the agent will be allowed to do with the path.

    ``access`` carries the agent's requested access mode (``READ`` or
    ``WRITE``) verbatim; ``access_human_label`` is the lower-case
    human-readable rendering (``"read-only"`` / ``"read & write"``)
    used in the fragment body.

    ``allowed_roots_json`` is a JSON array of the absolute WebDAV mount
    roots (home + temp); the dialog embeds it so the inbox shell can give
    instant client-side feedback (and disable Approve) when the edited
    path falls outside them, mirroring the server-side check.

    ``home_dir`` is the absolute home directory; the dialog embeds it so
    the inbox shell can expand a leading ``~`` / ``~/`` the user types
    before the within-roots check, mirroring the server-side expansion.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the fragment points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return CATALOG.render(
        "pages.LatchkeyFileSharingPermission",
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        file_path=file_path,
        access=access,
        access_human_label=access_human_label,
        allowed_roots_json=allowed_roots_json,
        home_dir=home_dir,
        display_name=file_path,
        mngr_forward_origin=mngr_forward_origin,
    )
