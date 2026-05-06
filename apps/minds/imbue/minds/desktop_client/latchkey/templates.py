"""Latchkey-specific HTML rendering.

The generic permission dialog (``templates/permissions.html``) is
backend-agnostic: it provides page chrome, header, rationale, form
scaffolding, action buttons, and submission JS that any permission
backend can inherit. This module wraps the latchkey-specific child
template (``templates/latchkey_permissions.html``) -- which fills the
generic blocks with a checkbox per detent permission schema and the
auth-browser progress notice -- in a typed render function.

Living next to ``latchkey/permissions.py`` (rather than in the shared
``desktop_client/templates.py``) keeps the latchkey-shaped function
signature -- which takes ``ServicePermissionInfo`` -- out of the generic
template module.
"""

from collections.abc import Sequence

from imbue.imbue_common.pure import pure
from imbue.minds.desktop_client.latchkey.services_catalog import ServicePermissionInfo
from imbue.minds.desktop_client.templates import JINJA_ENV
from imbue.minds.desktop_client.templates import workspace_accent


@pure
def render_latchkey_permission_dialog(
    agent_id: str,
    request_id: str,
    ws_name: str,
    rationale: str,
    service: ServicePermissionInfo,
    checked_permissions: Sequence[str],
    will_open_browser: bool,
    mngr_forward_origin: str = "",
) -> str:
    """Render the latchkey permission approval dialog HTML.

    ``will_open_browser`` controls the in-progress notice shown after the
    user clicks Approve: when True (latchkey will run ``auth browser``),
    the notice tells the user to expect a browser pop-up; when False
    (credentials are already valid, or the service requires manual
    credentials), it shows a generic ``Granting permission...`` message.

    ``mngr_forward_origin`` is the bare origin of the ``mngr forward`` plugin;
    the workspace link in the dialog points at ``{mngr_forward_origin}/goto/<agent>/``.
    """
    return JINJA_ENV.get_template("latchkey_permissions.html").render(
        agent_id=agent_id,
        request_id=request_id,
        ws_name=ws_name,
        rationale=rationale,
        display_name=service.display_name,
        permission_schemas=service.permission_schemas,
        checked_permissions=set(checked_permissions),
        accent=workspace_accent(agent_id),
        will_open_browser=will_open_browser,
        mngr_forward_origin=mngr_forward_origin,
    )
