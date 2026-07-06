"""Data-format migration 1: fold the ``minds-workspaces`` scope into ``latchkey-self``.

Earlier builds gave the cross-workspace management API its own ``minds-workspaces``
detent scope. Because that scope and the agent baseline's ``latchkey-self`` scope
both match on the gateway-self ``domain`` alone, two same-domain rules ended up in
a host's ``latchkey_permissions.json``, and detent's first-matching-scope-wins
evaluation made the rule *order* load-bearing (a domain-only catch-all placed first
would shadow and veto the narrower grant). The workspace verbs now attach as
permissions on the single ``latchkey-self`` scope instead -- like file-sharing and
accounts -- so there is only ever one gateway-self rule and order is irrelevant.

This migration rewrites existing per-host permissions files into the new shape:
``apply_up`` unions each file's ``minds-workspaces`` permission names onto its
``latchkey-self`` rule and drops the now-defunct ``minds-workspaces`` rule and
scope schema; ``apply_down`` reverses it, moving the workspace verb permissions
back into a dedicated ``minds-workspaces`` rule and reconstructing its scope
schema. The per-verb permission schemas (keyed by verb name) are untouched in
both directions -- only which scope rule references them changes.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import JsonValue

from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.migrations.interface import DataFormatMigration
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import list_host_permissions_paths
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import save_permissions

# Type of the pure per-file transform each migration direction dispatches to.
_ConfigTransform = Callable[[LatchkeyPermissionsConfig], LatchkeyPermissionsConfig]

# Frozen snapshot of the pre-migration on-disk format. These are intentionally
# hardcoded here rather than imported from the live workspace catalog: a
# migration must reproduce the exact historical shape it converts to/from, so it
# must not drift if the catalog's values ever change. ``_LEGACY_WORKSPACES_SCOPE``
# is the detent scope key + schema name the old build used; the gateway-self host
# and path prefix reconstruct that scope schema's ``domain``/``path`` gate.
_LEGACY_WORKSPACES_SCOPE: Final[str] = "minds-workspaces"
_LEGACY_GATEWAY_SELF_HOST: Final[str] = "latchkey-self.invalid"
_LEGACY_WORKSPACES_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/workspaces"

# The detent scope the cross-workspace verbs attach to in the new format. Defined
# locally (rather than imported from ``agent_setup``) to keep the migration layer
# free of any dependency on the agent-setup module, which itself imports ``core``
# -- ``core`` runs this migration, so importing it here would form a cycle.
_SCOPE_LATCHKEY_SELF: Final[str] = "latchkey-self"

# Shared prefix of every cross-workspace verb permission name (base verbs like
# ``minds-workspaces-read`` and per-target names like
# ``minds-workspaces-destroy-<id>``). Used to recognize which permissions on the
# ``latchkey-self`` rule belong to the workspace API when reverting.
_WORKSPACE_PERMISSION_PREFIX: Final[str] = f"{_LEGACY_WORKSPACES_SCOPE}-"


@pure
def _union_preserving_order(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """Return ``existing`` followed by every entry of ``added`` not already present."""
    result = list(existing)
    for permission in added:
        if permission not in result:
            result.append(permission)
    return tuple(result)


@pure
def fold_workspace_scope_into_latchkey_self(config: LatchkeyPermissionsConfig) -> LatchkeyPermissionsConfig:
    """Union any ``minds-workspaces`` rule's permissions onto ``latchkey-self`` and drop the scope.

    A no-op (returns an equal config) when the file has no ``minds-workspaces``
    rule, so re-running the migration is safe.
    """
    workspace_permissions: tuple[str, ...] = tuple(
        permission
        for rule in config.rules
        if _LEGACY_WORKSPACES_SCOPE in rule
        for permission in rule[_LEGACY_WORKSPACES_SCOPE]
    )
    if not workspace_permissions:
        return config

    # Rebuild the rules: drop every ``minds-workspaces`` rule and fold its
    # permissions onto the ``latchkey-self`` rule (in place if present).
    rebuilt_rules: list[dict[str, list[str]]] = []
    is_latchkey_self_seen = False
    for rule in config.rules:
        if _LEGACY_WORKSPACES_SCOPE in rule:
            continue
        if _SCOPE_LATCHKEY_SELF in rule:
            is_latchkey_self_seen = True
            merged = _union_preserving_order(tuple(rule[_SCOPE_LATCHKEY_SELF]), workspace_permissions)
            rebuilt_rules.append({_SCOPE_LATCHKEY_SELF: list(merged)})
        else:
            rebuilt_rules.append(dict(rule))
    if not is_latchkey_self_seen:
        rebuilt_rules.append({_SCOPE_LATCHKEY_SELF: list(workspace_permissions)})

    # Drop the now-defunct ``minds-workspaces`` scope schema; the per-verb
    # permission schemas (keyed by verb name) stay, still referenced above.
    rebuilt_schemas = {name: schema for name, schema in config.schemas.items() if name != _LEGACY_WORKSPACES_SCOPE}
    return LatchkeyPermissionsConfig(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


@pure
def _build_minds_workspaces_scope_schema() -> dict[str, JsonValue]:
    """Reconstruct the legacy ``minds-workspaces`` scope schema (domain + path-prefix gate)."""
    return {
        "properties": {
            "domain": {"const": _LEGACY_GATEWAY_SELF_HOST},
            "path": {"type": "string", "pattern": f"^{_LEGACY_WORKSPACES_PATH_PREFIX}(/|$)"},
        },
        "required": ["domain", "path"],
    }


@pure
def split_workspace_scope_out_of_latchkey_self(config: LatchkeyPermissionsConfig) -> LatchkeyPermissionsConfig:
    """Move the workspace verb permissions off ``latchkey-self`` into a ``minds-workspaces`` rule.

    The inverse of :func:`fold_workspace_scope_into_latchkey_self`. A no-op when
    the ``latchkey-self`` rule carries no workspace verb permissions.
    """
    workspace_permissions: tuple[str, ...] = tuple(
        permission
        for rule in config.rules
        if _SCOPE_LATCHKEY_SELF in rule
        for permission in rule[_SCOPE_LATCHKEY_SELF]
        if permission.startswith(_WORKSPACE_PERMISSION_PREFIX)
    )
    if not workspace_permissions:
        return config

    # Strip the workspace permissions off ``latchkey-self`` and insert a dedicated
    # ``minds-workspaces`` rule immediately before it (the canonical legacy order:
    # the narrower same-domain rule ahead of the domain-only catch-all).
    rebuilt_rules: list[dict[str, list[str]]] = []
    for rule in config.rules:
        if _SCOPE_LATCHKEY_SELF in rule:
            remaining = [p for p in rule[_SCOPE_LATCHKEY_SELF] if not p.startswith(_WORKSPACE_PERMISSION_PREFIX)]
            rebuilt_rules.append({_LEGACY_WORKSPACES_SCOPE: list(workspace_permissions)})
            rebuilt_rules.append({_SCOPE_LATCHKEY_SELF: remaining})
        else:
            rebuilt_rules.append(dict(rule))

    rebuilt_schemas: dict[str, JsonValue] = dict(config.schemas)
    rebuilt_schemas[_LEGACY_WORKSPACES_SCOPE] = _build_minds_workspaces_scope_schema()
    return LatchkeyPermissionsConfig(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


class FoldWorkspaceScopeIntoLatchkeySelf(DataFormatMigration):
    """Rewrite every per-host permissions file between the two-scope and single-scope layouts."""

    def apply_up(self, plugin_data_dir: Path) -> None:
        self._rewrite_each_host_file(plugin_data_dir, fold_workspace_scope_into_latchkey_self)

    def apply_down(self, plugin_data_dir: Path) -> None:
        self._rewrite_each_host_file(plugin_data_dir, split_workspace_scope_out_of_latchkey_self)

    def _rewrite_each_host_file(
        self,
        plugin_data_dir: Path,
        transform: _ConfigTransform,
    ) -> None:
        for path in list_host_permissions_paths(plugin_data_dir):
            config = load_permissions(path)
            transformed = transform(config)
            if transformed != config:
                logger.debug("Migrating permissions file {} for data-format change", path)
                save_permissions(path, transformed)
