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

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.templates import JINJA_ENV
from imbue.minds.desktop_client.templates import workspace_accent


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
    """Render the predefined (catalog-backed) permission approval dialog.

    ``will_open_browser`` controls the in-progress notice shown after the
    user clicks Approve: when True (latchkey will run ``auth browser``),
    the notice tells the user to expect a browser pop-up; when False
    (credentials are already valid, or the service requires manual
    credentials), it shows a generic ``Granting permission...`` message.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the dialog points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return JINJA_ENV.get_template("latchkey_predefined_permission.html").render(
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        display_name=service.display_name,
        scope=service.scope,
        scope_description=service.description,
        permission_schemas=service.permission_schemas,
        description_by_permission_name=service.description_by_permission_name,
        checked_permissions=set(checked_permissions),
        accent=workspace_accent(agent_id),
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
    mngr_forward_origin: str = "",
) -> str:
    """Render the file-sharing permission approval dialog.

    Mirrors the predefined dialog's chrome, header, rationale card, and
    submission JS (via the shared ``templates/permissions.html`` base);
    swaps the per-permission checkbox list for a short explanation of
    what the agent will be allowed to do with the path.

    ``access`` carries the agent's requested access mode (``READ`` or
    ``WRITE``) verbatim; ``access_human_label`` is the lower-case
    human-readable rendering (``"read-only"`` / ``"read & write"``)
    used in the dialog body.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the dialog points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return JINJA_ENV.get_template("latchkey_file_sharing_permission.html").render(
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        file_path=file_path,
        access=access,
        access_human_label=access_human_label,
        display_name=file_path,
        accent=workspace_accent(agent_id),
        mngr_forward_origin=mngr_forward_origin,
    )
