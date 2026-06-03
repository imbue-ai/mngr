"""Latchkey permission-dialog HTML rendering.

The generic permission dialog (``templates/permissions.html``) is
backend-agnostic: it provides page chrome, header, rationale, form
scaffolding, action buttons, and submission JS that any permission
backend can inherit. This module wraps the two latchkey-specific child
templates -- one per sibling handler in this package -- in typed render
functions:

* :func:`render_predefined_permission_dialog` wraps
  ``templates/latchkey_predefined_permission.html``, which fills the
  generic blocks with a checkbox per detent permission schema and the
  auth-browser progress notice;
* :func:`render_file_sharing_permission_dialog` wraps
  ``templates/latchkey_file_sharing_permission.html``, which fills the
  generic blocks with a single hidden ``approve=yes`` checkbox so the
  base form's submit handler treats the dialog as a plain yes/no.

Keeping these renderers next to the handlers (rather than in the shared
``desktop_client/templates.py``) keeps the latchkey-shaped function
signatures -- notably the one that takes ``ServicePermissionInfo`` --
out of the generic template module.
"""

from collections.abc import Sequence

from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.ssr_sidecar import SsrSidecar
from imbue.minds.desktop_client.templates import render_ssr_or_fallback
from imbue.minds.desktop_client.templates import workspace_accent


def render_predefined_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    service: ServicePermissionInfo,
    checked_permissions: Sequence[str],
    will_open_browser: bool,
    mngr_forward_origin: str = "",
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the predefined (catalog-backed) permission approval dialog.

    ``will_open_browser`` controls the in-progress notice shown after the
    user clicks Approve: when True (latchkey will run ``auth browser``),
    the notice tells the user to expect a browser pop-up; when False
    (credentials are already valid, or the service requires manual
    credentials), it shows a generic ``Granting permission...`` message.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the dialog points at ``{mngr_forward_origin}/goto/<agent>/``.

    Implemented as a Solid component (``routes/permissions/predefined.jsx``);
    this shim asks the SSR sidecar to render it and falls back to the
    client-render shell when the sidecar isn't available.
    """
    return render_ssr_or_fallback(
        sidecar=sidecar,
        route="permissions/predefined",
        props={
            "agentId": agent_id,
            "requestId": request_id,
            "wsName": ws_name,
            "rationale": rationale,
            "displayName": service.display_name,
            "scope": service.scope,
            "permissionSchemas": list(service.permission_schemas),
            "descriptionByPermissionName": dict(service.description_by_permission_name),
            "checkedPermissions": sorted(set(checked_permissions)),
            "accent": workspace_accent(agent_id),
            "willOpenBrowser": will_open_browser,
            "mngrForwardOrigin": mngr_forward_origin,
        },
    )


def render_file_sharing_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    file_path: str,
    access: str,
    access_human_label: str,
    mngr_forward_origin: str = "",
    sidecar: SsrSidecar | None = None,
) -> str:
    """Render the file-sharing permission approval dialog.

    Mirrors the predefined dialog's chrome, header, rationale card, and
    submission JS (via the shared ``permissions/PermissionRequest`` Solid
    component); swaps the per-permission checkbox list for a short
    explanation of what the agent will be allowed to do with the path.

    ``access`` carries the agent's requested access mode (``READ`` or
    ``WRITE``) verbatim; ``access_human_label`` is the lower-case
    human-readable rendering (``"read-only"`` / ``"read & write"``)
    used in the dialog body.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the dialog points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return render_ssr_or_fallback(
        sidecar=sidecar,
        route="permissions/file_sharing",
        props={
            "agentId": agent_id,
            "requestId": request_id,
            "wsName": ws_name,
            "rationale": rationale,
            "filePath": file_path,
            "access": access,
            "accessHumanLabel": access_human_label,
            "displayName": file_path,
            "accent": workspace_accent(agent_id),
            "mngrForwardOrigin": mngr_forward_origin,
        },
    )
