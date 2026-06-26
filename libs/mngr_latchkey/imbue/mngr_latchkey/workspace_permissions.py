"""Verb metadata for the cross-workspace ``minds-workspaces`` API.

Minds exposes a small cross-workspace management API
(``/api/v1/workspaces/...``) that an agent in one workspace can call to act on
*other* workspaces -- listing them, reading detail, creating, destroying,
starting/stopping, exporting backups, and establishing SSH access. Those calls
are reached through the gateway's bundled ``minds-api-proxy`` extension (so the
detent envelope's domain is the synthetic ``latchkey-self.invalid`` gateway-self
host) and gated by a single ``minds-workspaces`` detent scope with one named
permission per verb.

The verb catalog -- the scope name, the verb permission-schema names, their
targeted/non-targeted split, and the dialog labels -- lives in a single shared
data file, ``extensions/workspace_permissions.json``, read by *both* this module
(for the dialog-facing metadata) and the gateway's ``permission_requests.mjs``
extension (for the request-path schema construction). Keeping one source of
truth means the two sides cannot drift; an integration check in
``permission_requests_test.py`` confirms the gateway accepts exactly the verbs
this module exposes.

The actual permission *effect* (the scope + per-verb schemas + the grant rule)
is computed in that gateway extension and applied through the standard
``POST /permission-requests/approve`` path, so no schema construction lives here.

The verbs split on a target axis:

* ``read`` and ``create`` are all-or-nothing (listing is not per-workspace and
  create takes no target).
* ``destroy``, ``lifecycle``, ``backups-export``, and ``ssh`` are target-scoped:
  each approval mints a uniquely-named per-target verb schema, so granting
  access to another workspace accumulates rather than replaces.
"""

import json
from collections.abc import Mapping
from functools import cache
from importlib import resources
from typing import Final

from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel

# The shared verb-catalog data file, shipped beside the gateway extensions (and
# copied into the gateway's extension directory at spawn time alongside
# ``services.json``).
_EXTENSIONS_PACKAGE: Final[str] = "imbue.mngr_latchkey.extensions"
_WORKSPACE_PERMISSIONS_FILENAME: Final[str] = "workspace_permissions.json"


class WorkspacePermissionsError(RuntimeError):
    """Raised when the bundled ``workspace_permissions.json`` is missing or malformed.

    Trusted package data, so any structural problem is a packaging bug.
    """


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


class _WorkspacePermissionsCatalog(FrozenModel):
    """Parsed view of the shared verb-catalog data file."""

    scope: str = Field(description="Detent scope schema name for the cross-workspace API.")
    verbs: tuple[WorkspaceVerb, ...] = Field(description="The grantable verbs, in dialog order.")


@cache
def _load_catalog() -> _WorkspacePermissionsCatalog:
    """Read, validate, and translate the shared catalog (cached once per process)."""
    resource = resources.files(_EXTENSIONS_PACKAGE).joinpath(_WORKSPACE_PERMISSIONS_FILENAME)
    try:
        raw = resource.read_text(encoding="utf-8")
    except OSError as e:
        raise WorkspacePermissionsError(f"Could not read bundled {_WORKSPACE_PERMISSIONS_FILENAME}: {e}") from e
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise WorkspacePermissionsError(f"Bundled {_WORKSPACE_PERMISSIONS_FILENAME} is not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise WorkspacePermissionsError(f"{_WORKSPACE_PERMISSIONS_FILENAME} top-level value must be a JSON object.")
    scope = parsed.get("scope")
    raw_verbs = parsed.get("verbs")
    if not isinstance(scope, str) or not scope:
        raise WorkspacePermissionsError(f"{_WORKSPACE_PERMISSIONS_FILENAME}: 'scope' must be a non-empty string.")
    if not isinstance(raw_verbs, list) or not raw_verbs:
        raise WorkspacePermissionsError(f"{_WORKSPACE_PERMISSIONS_FILENAME}: 'verbs' must be a non-empty array.")
    verbs: list[WorkspaceVerb] = []
    for entry in raw_verbs:
        if not isinstance(entry, dict):
            raise WorkspacePermissionsError(f"{_WORKSPACE_PERMISSIONS_FILENAME}: each verb must be a JSON object.")
        path = entry.get("path")
        if not isinstance(path, dict) or not isinstance(path.get("kind"), str):
            raise WorkspacePermissionsError(
                f"{_WORKSPACE_PERMISSIONS_FILENAME}: verb {entry.get('permission')!r} needs a 'path.kind'."
            )
        verbs.append(
            WorkspaceVerb(
                permission=_require_str(entry, "permission"),
                display_name=_require_str(entry, "display_name"),
                description=_require_str(entry, "description"),
                is_targeted=path["kind"] == "targeted",
            )
        )
    return _WorkspacePermissionsCatalog(scope=scope, verbs=tuple(verbs))


def _require_str(entry: Mapping[str, object], field: str) -> str:
    value = entry.get(field)
    if not isinstance(value, str) or not value:
        raise WorkspacePermissionsError(
            f"{_WORKSPACE_PERMISSIONS_FILENAME}: verb is missing a non-empty string '{field}'."
        )
    return value


# Module-level views of the shared catalog, resolved once at import. The data
# file is trusted package data that is always present, so a load failure is a
# packaging bug and surfaces immediately as :class:`WorkspacePermissionsError`.
_CATALOG: Final[_WorkspacePermissionsCatalog] = _load_catalog()

# Detent scope schema for the cross-workspace API. Appears as the rule key in a
# per-host ``latchkey_permissions.json`` (``{"minds-workspaces": [...]}``) and as
# the ``scope`` a workspace permission request is filed under.
MINDS_WORKSPACES_SCOPE: Final[str] = _CATALOG.scope

# The grantable verbs, in the order the dialog presents them.
WORKSPACE_VERBS: Final[tuple[WorkspaceVerb, ...]] = _CATALOG.verbs


def is_targeted_verb(permission: str) -> bool:
    """Whether ``permission`` is a target-scoped verb (gated per-target workspace)."""
    return any(verb.permission == permission and verb.is_targeted for verb in WORKSPACE_VERBS)
