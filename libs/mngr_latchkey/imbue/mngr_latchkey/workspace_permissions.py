"""Per-target permission grants for the cross-workspace ``minds-workspaces`` API.

Minds exposes a small cross-workspace management API
(``/api/v1/workspaces/...``) that an agent in one workspace can call to act on
*other* workspaces -- listing them, reading detail, creating, destroying,
starting/stopping, exporting backups, and establishing SSH access. Those calls
are reached through the gateway's bundled ``minds-api-proxy`` extension (so the
detent envelope's domain is the synthetic ``latchkey-self.invalid`` gateway-self
host) and gated by a single ``minds-workspaces`` detent scope with one named
permission per verb.

This module owns:

* the verb catalog (the permission-schema names, their HTTP method + path shape,
  and dialog-facing labels), shared by both the agent baseline and the desktop
  permission dialog;
* the *non-targeted* baseline schemas (the scope gate plus the broad ``read`` and
  ``create`` verbs), which the agent baseline materializes in every per-host file;
* :func:`grant_workspace_permissions`, which applies a user-approved grant by
  unioning the granted verbs into the host's ``minds-workspaces`` rule and -- for
  the *targeted* verbs (destroy / lifecycle / backups-export / ssh) --
  accumulating the approved target workspace id into that verb's permission
  schema as an ``anyOf`` of path patterns.

The ``anyOf`` accumulation generalizes the per-agent allowlist machinery in
:mod:`imbue.mngr_latchkey.agent_setup` (the ``minds-api-proxy`` ``not.anyOf``
block): there the path schema carries the set of *agent ids* allowed past an
unauthorized gate; here each targeted verb's path schema carries the set of
*target workspace ids* the caller may act on. Listing (``read``) and ``create``
stay all-or-nothing -- they are not target-scoped.

The targeted verb schemas are *not* part of the baseline: a fresh host file has
no targeted-verb access at all, and the first approved grant for a given verb
creates that verb's schema with the single approved target in its ``anyOf``.
Subsequent grants append to the same ``anyOf``. This keeps the deny-by-default
baseline free of an empty ``anyOf`` (which JSON-Schema validators reject).
"""

import re
from collections.abc import Sequence
from pathlib import Path
from typing import Final

from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions

# The gateway-self synthetic host every ``minds-api-proxy`` request carries as
# its detent ``domain`` (mirrors ``agent_setup._GATEWAY_SELF_HOST``).
_GATEWAY_SELF_HOST: Final[str] = "latchkey-self.invalid"

# Detent scope schema for the cross-workspace API. Appears as the rule key in a
# per-host ``latchkey_permissions.json`` (``{"minds-workspaces": [...]}``) and as
# the ``scope`` an agent's workspace permission request carries.
MINDS_WORKSPACES_SCOPE: Final[str] = "minds-workspaces"

# URL path prefix the cross-workspace API lives under, as seen by the gateway
# (the ``minds-api-proxy`` mount + the minds ``/api/v1/workspaces`` routes).
MINDS_WORKSPACES_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/workspaces"

# Verb permission-schema names. Each names one Detent permission schema under the
# ``minds-workspaces`` scope.
PERM_WORKSPACES_READ: Final[str] = "minds-workspaces-read"
PERM_WORKSPACES_CREATE: Final[str] = "minds-workspaces-create"
PERM_WORKSPACES_DESTROY: Final[str] = "minds-workspaces-destroy"
PERM_WORKSPACES_LIFECYCLE: Final[str] = "minds-workspaces-lifecycle"
PERM_WORKSPACES_BACKUPS_EXPORT: Final[str] = "minds-workspaces-backups-export"
PERM_WORKSPACES_SSH: Final[str] = "minds-workspaces-ssh"

# Scope-level path-prefix gate. Necessary because detent ``any`` matches every
# request satisfying the scope schema -- without this gate, a broad grant would
# escape into every other gateway-self endpoint. Matches the collection root and
# anything beneath it.
_SCOPE_PATTERN: Final[str] = rf"^{MINDS_WORKSPACES_PATH_PREFIX}(/|$)"

# A single path segment (one workspace id, operation id, or snapshot id) -- the
# "all workspaces" id wildcard used when a targeted verb is granted broadly.
_ANY_SEGMENT: Final[str] = r"[^/]+"

# Characters allowed verbatim in a workspace (agent) id when embedded into a
# regex pattern body. mngr's ``RandomId`` format -- ``<prefix>-<32 hex>`` -- only
# uses ``[a-z0-9-]``, all regex-safe outside a character class. We validate
# explicitly (rather than trusting the caller's typing) so a future id-shape
# change cannot silently inject regex metacharacters into the on-disk pattern.
# Mirrors ``agent_setup._SAFE_AGENT_ID_RE``.
_SAFE_WORKSPACE_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z0-9_-]+$")


class WorkspaceVerb(FrozenModel):
    """One grantable verb under the ``minds-workspaces`` scope.

    ``permission`` is the Detent permission-schema name (e.g.
    ``minds-workspaces-destroy``) that appears in a host file's rule and that the
    desktop dialog offers as a checkbox. ``is_targeted`` is ``True`` for the
    verbs whose request path carries a target workspace id (destroy, lifecycle,
    backups-export, ssh): those accumulate a per-target ``anyOf`` allowlist. The
    non-targeted verbs (read, create) are all-or-nothing.
    """

    permission: str = Field(description="Detent permission-schema name for this verb.")
    display_name: str = Field(description="Human-readable label shown in the permission dialog.")
    description: str = Field(description="Plain-English summary of what the verb allows.")
    is_targeted: bool = Field(
        description="Whether the verb is scoped to a target workspace id (accumulated in an ``anyOf``).",
    )


# The verbs in the order the dialog presents them. The list verb (``read``)
# comes first, then create, then the target-scoped mutating verbs.
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

# Path-pattern suffixes (relative to ``<prefix>/<id>``) for the targeted verbs.
# Combined with the id segment to form each verb's full ``^...$`` path pattern.
_TARGETED_VERB_PATH_SUFFIX: Final[dict[str, str]] = {
    PERM_WORKSPACES_DESTROY: r"/destroy$",
    PERM_WORKSPACES_LIFECYCLE: r"/(start|stop)$",
    PERM_WORKSPACES_BACKUPS_EXPORT: rf"/backups/{_ANY_SEGMENT}/export$",
    PERM_WORKSPACES_SSH: r"/ssh$",
}


def _scope_schema() -> dict[str, JsonValue]:
    """Build the ``minds-workspaces`` scope schema (domain + path-prefix gate)."""
    return {
        "properties": {
            "domain": {"const": _GATEWAY_SELF_HOST},
            "path": {"type": "string", "pattern": _SCOPE_PATTERN},
        },
        "required": ["domain", "path"],
    }


def _read_schema() -> dict[str, JsonValue]:
    """Build the broad ``read`` verb schema (any GET under the workspaces tree).

    Read covers every GET under ``/workspaces`` (list, detail, version, backups
    listing, and operation status/logs), so an agent that can read can also watch
    the operations spawned by its own create/destroy calls. Listing is
    all-or-nothing; it is not target-scoped.
    """
    return {
        "properties": {
            "method": {"const": "GET"},
            "path": {"type": "string", "pattern": _SCOPE_PATTERN},
        },
        "required": ["method", "path"],
    }


def _create_schema() -> dict[str, JsonValue]:
    """Build the ``create`` verb schema (the exact collection POST)."""
    return {
        "properties": {
            "method": {"const": "POST"},
            "path": {"const": MINDS_WORKSPACES_PATH_PREFIX},
        },
        "required": ["method", "path"],
    }


# Baseline (non-targeted) schemas materialized in every per-host permissions
# file: the scope gate plus the broad read + create verbs. The targeted verb
# schemas are deliberately absent -- they are created on first grant by
# :func:`grant_workspace_permissions` so the baseline never carries an empty
# ``anyOf``.
WORKSPACE_BASELINE_SCHEMAS: Final[dict[str, JsonValue]] = {
    MINDS_WORKSPACES_SCOPE: _scope_schema(),
    PERM_WORKSPACES_READ: _read_schema(),
    PERM_WORKSPACES_CREATE: _create_schema(),
}

# The baseline schema keys, in a stable order. Used by the startup migration to
# detect a permissions file that predates this scope.
WORKSPACE_BASELINE_SCHEMA_KEYS: Final[tuple[str, ...]] = (
    MINDS_WORKSPACES_SCOPE,
    PERM_WORKSPACES_READ,
    PERM_WORKSPACES_CREATE,
)


def is_targeted_verb(permission: str) -> bool:
    """Whether ``permission`` is a target-scoped verb (accumulates an ``anyOf``)."""
    verb = _VERB_BY_PERMISSION.get(permission)
    return verb is not None and verb.is_targeted


def _validate_workspace_id_for_pattern(workspace_id: str) -> str:
    """Return ``workspace_id`` if it is safe to embed in a regex, else raise."""
    if not _SAFE_WORKSPACE_ID_RE.match(workspace_id):
        raise LatchkeyStoreError(
            f"workspace id contains characters that are not safe to embed in a regex: {workspace_id!r}"
        )
    return workspace_id


def _targeted_verb_path_pattern(permission: str, id_segment: str) -> str:
    """Build the full ``^...$`` path pattern for a targeted verb and id segment.

    ``id_segment`` is either the "any workspace" wildcard (``[^/]+``) for an
    all-workspaces grant, or a single validated workspace id for a selected
    grant.
    """
    suffix = _TARGETED_VERB_PATH_SUFFIX[permission]
    return rf"^{MINDS_WORKSPACES_PATH_PREFIX}/{id_segment}{suffix}"


def _targeted_verb_anyof_entry(permission: str, target_workspace_id: AgentId | None) -> dict[str, JsonValue]:
    """Build the ``anyOf`` entry granting a targeted verb for one target (or all).

    ``target_workspace_id is None`` means "all workspaces": the entry uses the
    ``[^/]+`` id wildcard. Otherwise it pins the single approved workspace id.
    """
    if target_workspace_id is None:
        id_segment = _ANY_SEGMENT
    else:
        id_segment = _validate_workspace_id_for_pattern(str(target_workspace_id))
    return {"pattern": _targeted_verb_path_pattern(permission, id_segment)}


def _build_targeted_verb_schema(permission: str, any_of_entries: Sequence[JsonValue]) -> dict[str, JsonValue]:
    """Build a targeted verb's permission schema from its accumulated ``anyOf`` entries."""
    return {
        "properties": {
            "method": {"const": "POST"},
            "path": {"type": "string", "anyOf": list(any_of_entries)},
        },
        "required": ["method", "path"],
    }


def _existing_targeted_verb_anyof(schema: JsonValue) -> list[JsonValue]:
    """Recover the accumulated ``anyOf`` entries from an existing targeted verb schema.

    Returns an empty list when ``schema`` is absent or does not carry a
    well-formed ``properties.path.anyOf`` list (e.g. a stale broad-pattern schema
    from before per-target gating), so the next grant rebuilds it cleanly from
    the canonical structure.
    """
    if not isinstance(schema, dict):
        return []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return []
    path_schema = properties.get("path")
    if not isinstance(path_schema, dict):
        return []
    any_of = path_schema.get("anyOf")
    if not isinstance(any_of, list):
        return []
    return list(any_of)


def _merge_rule_permissions(
    rules: Sequence[dict[str, list[str]]],
    granted_verbs: Sequence[str],
) -> tuple[dict[str, list[str]], ...]:
    """Union ``granted_verbs`` into the ``minds-workspaces`` rule, preserving order."""
    new_rules = [dict(rule) for rule in rules]
    scope_index = next(
        (index for index, rule in enumerate(new_rules) if list(rule.keys()) == [MINDS_WORKSPACES_SCOPE]),
        None,
    )
    if scope_index is None:
        new_rules.append({MINDS_WORKSPACES_SCOPE: list(granted_verbs)})
        return tuple(new_rules)
    existing = list(new_rules[scope_index][MINDS_WORKSPACES_SCOPE])
    for verb in granted_verbs:
        if verb not in existing:
            existing.append(verb)
    new_rules[scope_index] = {MINDS_WORKSPACES_SCOPE: existing}
    return tuple(new_rules)


def grant_workspace_permissions(
    plugin_data_dir: Path,
    host_id: HostId,
    granted_verbs: Sequence[str],
    target_workspace_id: AgentId | None,
) -> None:
    """Apply a user-approved cross-workspace grant to a host's permissions file.

    ``host_id`` is the *requesting* agent's host (every agent on a host shares one
    ``latchkey_permissions.json``); ``granted_verbs`` are the verb permission
    names the user approved; ``target_workspace_id`` is the workspace the targeted
    verbs apply to, or ``None`` for an "all workspaces" grant.

    For each granted verb the verb permission is unioned into the host's
    ``minds-workspaces`` rule. The non-targeted verbs (read, create) also get
    their baseline schema ensured present. The targeted verbs (destroy,
    lifecycle, backups-export, ssh) additionally accumulate the approved target
    (or the all-workspaces wildcard) into that verb's permission schema as an
    ``anyOf`` of path patterns -- idempotently, so re-approving the same target
    is a no-op append.

    The host permissions file must already exist (a live agent that can file a
    permission request always has one); :class:`LatchkeyStoreError` is raised
    otherwise, and for any unknown verb name.
    """
    unknown = [verb for verb in granted_verbs if verb not in _VERB_BY_PERMISSION]
    if unknown:
        raise LatchkeyStoreError(f"Unknown minds-workspaces verb(s): {unknown}")
    if not granted_verbs:
        raise LatchkeyStoreError("grant_workspace_permissions requires at least one verb")

    path = permissions_path_for_host(plugin_data_dir, host_id)
    if not path.is_file():
        raise LatchkeyStoreError(
            f"Host permissions file {path} does not exist; cannot grant minds-workspaces permissions."
        )
    config = load_permissions(path)

    schemas: dict[str, JsonValue] = dict(config.schemas)
    # The scope gate must always be present so detent can resolve the rule key.
    schemas.setdefault(MINDS_WORKSPACES_SCOPE, _scope_schema())

    for verb in granted_verbs:
        if not is_targeted_verb(verb):
            # Non-targeted verbs (read, create) carry a fixed broad schema; ensure
            # it is present (it normally is, via the baseline migration).
            schemas.setdefault(verb, WORKSPACE_BASELINE_SCHEMAS[verb])
            continue
        existing_entries = _existing_targeted_verb_anyof(schemas.get(verb))
        new_entry = _targeted_verb_anyof_entry(verb, target_workspace_id)
        if new_entry not in existing_entries:
            existing_entries.append(new_entry)
        schemas[verb] = _build_targeted_verb_schema(verb, existing_entries)

    new_rules = _merge_rule_permissions(config.rules, granted_verbs)
    save_permissions(path, LatchkeyPermissionsConfig(rules=new_rules, schemas=schemas))
