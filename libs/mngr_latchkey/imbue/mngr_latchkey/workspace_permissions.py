"""Verb metadata for the cross-workspace ``minds-workspaces`` API.

Minds exposes a small cross-workspace management API
(``/api/v1/workspaces/...``) that an agent in one workspace can call to act on
*other* workspaces -- listing them, reading detail, creating, destroying,
starting/stopping, exporting backups, and establishing SSH access. Those calls
are reached through the gateway's bundled ``minds-api-proxy`` extension (so the
detent envelope's domain is the synthetic ``latchkey-self.invalid`` gateway-self
host) and gated by a single ``minds-workspaces`` detent scope with one named
permission per verb.

This module is the Python-side source of truth for the *dialog-facing* verb
metadata (display labels, descriptions, and the targeted/non-targeted split):
the desktop permission dialog renders a checkbox per verb from
:data:`WORKSPACE_VERBS`.

The actual permission *effect* -- the scope + per-verb schemas and the grant
rule -- is computed in the gateway's ``permission_requests.mjs`` extension and
applied through the standard ``POST /permission-requests/approve`` path (exactly
like file-sharing), so the schema construction lives there, not here. The verb
names and the targeted/non-targeted classification MUST stay in sync with that
extension's ``WORKSPACE_VERB_DEFS``; a cross-language drift guard lives in
``permission_requests_test.py``.

The verbs split on a target axis:

* ``read`` and ``create`` are all-or-nothing (listing is not per-workspace and
  create takes no target).
* ``destroy``, ``lifecycle``, ``backups-export``, and ``ssh`` are target-scoped:
  each approval mints a uniquely-named per-target verb schema, so granting
  access to another workspace accumulates rather than replaces.
"""

from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# Detent scope schema for the cross-workspace API. Appears as the rule key in a
# per-host ``latchkey_permissions.json`` (``{"minds-workspaces": [...]}``) and as
# the ``scope`` a workspace permission request is filed under.
MINDS_WORKSPACES_SCOPE: Final[str] = "minds-workspaces"

# Verb permission-schema names. Each names one Detent permission schema under the
# ``minds-workspaces`` scope.
PERM_WORKSPACES_READ: Final[str] = "minds-workspaces-read"
PERM_WORKSPACES_CREATE: Final[str] = "minds-workspaces-create"
PERM_WORKSPACES_DESTROY: Final[str] = "minds-workspaces-destroy"
PERM_WORKSPACES_LIFECYCLE: Final[str] = "minds-workspaces-lifecycle"
PERM_WORKSPACES_BACKUPS_EXPORT: Final[str] = "minds-workspaces-backups-export"
PERM_WORKSPACES_SSH: Final[str] = "minds-workspaces-ssh"


class WorkspaceVerb(FrozenModel):
    """One grantable verb under the ``minds-workspaces`` scope.

    ``permission`` is the Detent permission-schema name (e.g.
    ``minds-workspaces-destroy``) that the dialog offers as a checkbox.
    ``is_targeted`` is ``True`` for the verbs whose request path carries a target
    workspace id (destroy, lifecycle, backups-export, ssh): those are gated
    per-target. The non-targeted verbs (read, create) are all-or-nothing.
    """

    permission: str = Field(description="Detent permission-schema name for this verb.")
    display_name: str = Field(description="Human-readable label shown in the permission dialog.")
    description: str = Field(description="Plain-English summary of what the verb allows.")
    is_targeted: bool = Field(description="Whether the verb is scoped to a target workspace id.")


# The verbs in the order the dialog presents them.
WORKSPACE_VERBS: Final[tuple[WorkspaceVerb, ...]] = (
    WorkspaceVerb(
        permission=PERM_WORKSPACES_READ,
        display_name="List and read workspaces",
        description=(
            "List all workspaces and read their detail, version, and backups. Applies to all "
            "workspaces (listing is not per-workspace)."
        ),
        is_targeted=False,
    ),
    WorkspaceVerb(
        permission=PERM_WORKSPACES_CREATE,
        display_name="Create workspaces",
        description="Create new workspaces.",
        is_targeted=False,
    ),
    WorkspaceVerb(
        permission=PERM_WORKSPACES_DESTROY,
        display_name="Destroy a workspace",
        description="Permanently destroy the selected workspace.",
        is_targeted=True,
    ),
    WorkspaceVerb(
        permission=PERM_WORKSPACES_LIFECYCLE,
        display_name="Start/stop a workspace",
        description="Start or stop the selected workspace's host.",
        is_targeted=True,
    ),
    WorkspaceVerb(
        permission=PERM_WORKSPACES_BACKUPS_EXPORT,
        display_name="Export a workspace's backups",
        description="Export a backup snapshot of the selected workspace.",
        is_targeted=True,
    ),
    WorkspaceVerb(
        permission=PERM_WORKSPACES_SSH,
        display_name="Establish SSH access to a workspace",
        description="Establish SSH access into the selected workspace.",
        is_targeted=True,
    ),
)

_VERB_BY_PERMISSION: Final[dict[str, WorkspaceVerb]] = {verb.permission: verb for verb in WORKSPACE_VERBS}


def is_targeted_verb(permission: str) -> bool:
    """Whether ``permission`` is a target-scoped verb (gated per-target workspace)."""
    verb = _VERB_BY_PERMISSION.get(permission)
    return verb is not None and verb.is_targeted
