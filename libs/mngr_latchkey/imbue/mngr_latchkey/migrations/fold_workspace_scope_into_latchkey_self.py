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

Everything the migration needs -- the permissions-file model, the read/write, and
the per-host file walk -- is a small frozen copy local to this module rather than
an import from :mod:`imbue.mngr_latchkey.store`. A migration is a historical
artifact: pinning it to its own copies means it keeps performing the exact same
rewrite even if the live store model, its serialization, or the on-disk layout
later changes (such a change would ship as its own, later migration).
"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import ConfigDict
from pydantic import Field
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.migrations.interface import DataFormatMigration
from imbue.mngr_latchkey.migrations.interface import LatchkeyMigrationError

# Frozen snapshot of the pre-migration on-disk format. These are intentionally
# hardcoded here rather than imported from the live workspace catalog: a
# migration must reproduce the exact historical shape it converts to/from, so it
# must not drift if the catalog's values ever change. ``_LEGACY_WORKSPACES_SCOPE``
# is the detent scope key + schema name the old build used; the gateway-self host
# and path prefix reconstruct that scope schema's ``domain``/``path`` gate.
_LEGACY_WORKSPACES_SCOPE: Final[str] = "minds-workspaces"
_LEGACY_GATEWAY_SELF_HOST: Final[str] = "latchkey-self.invalid"
_LEGACY_WORKSPACES_PATH_PREFIX: Final[str] = "/minds-api-proxy/api/v1/workspaces"

# The detent scope the cross-workspace verbs attach to in the new format.
_SCOPE_LATCHKEY_SELF: Final[str] = "latchkey-self"

# Shared prefix of every cross-workspace verb permission name (base verbs like
# ``minds-workspaces-read`` and per-target names like
# ``minds-workspaces-destroy-<id>``). Used to recognize which permissions on the
# ``latchkey-self`` rule belong to the workspace API when reverting.
_WORKSPACE_PERMISSION_PREFIX: Final[str] = f"{_LEGACY_WORKSPACES_SCOPE}-"

# The per-host on-disk layout this migration walks. A frozen copy of the store's
# private layout constants so the migration does not ride on the live values.
_HOSTS_DIR_NAME: Final[str] = "hosts"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"


class _PermissionsFile(FrozenModel):
    """Migration-local, frozen view of a permissions file.

    A deliberately independent copy of the parts this migration reads and rewrites
    (the ``rules`` array and ``schemas`` object), so the migration keeps producing
    the same historical transform even if ``store.LatchkeyPermissionsConfig`` later
    changes. Unrecognized top-level keys are dropped on load (matching the store's
    own save behavior); nothing minds writes to these files lives outside the two
    modeled sections.
    """

    model_config = ConfigDict(extra="ignore")

    rules: tuple[dict[str, list[str]], ...] = Field(default_factory=tuple)
    schemas: dict[str, JsonValue] = Field(default_factory=dict)


# Type of the pure per-file transform each migration direction dispatches to.
_ConfigTransform = Callable[[_PermissionsFile], _PermissionsFile]


@pure
def _union_preserving_order(existing: tuple[str, ...], added: tuple[str, ...]) -> tuple[str, ...]:
    """Return ``existing`` followed by every entry of ``added`` not already present."""
    result = list(existing)
    for permission in added:
        if permission not in result:
            result.append(permission)
    return tuple(result)


@pure
def fold_workspace_scope_into_latchkey_self(config: _PermissionsFile) -> _PermissionsFile:
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
    return _PermissionsFile(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


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
def split_workspace_scope_out_of_latchkey_self(config: _PermissionsFile) -> _PermissionsFile:
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
    return _PermissionsFile(rules=tuple(rebuilt_rules), schemas=rebuilt_schemas)


def _iter_host_permission_files(plugin_data_dir: Path) -> list[Path]:
    """Return every existing per-host ``latchkey_permissions.json`` under ``plugin_data_dir``."""
    hosts_root = plugin_data_dir / _HOSTS_DIR_NAME
    if not hosts_root.is_dir():
        return []
    paths = [
        host_dir / _PERMISSIONS_FILENAME
        for host_dir in hosts_root.iterdir()
        if host_dir.is_dir() and (host_dir / _PERMISSIONS_FILENAME).is_file()
    ]
    return sorted(paths)


def _read_permissions_file(path: Path) -> _PermissionsFile:
    """Parse a permissions file into the migration-local model."""
    try:
        raw = path.read_text()
    except OSError as e:
        raise LatchkeyMigrationError(f"Failed to read permissions file {path} during migration: {e}") from e
    try:
        return _PermissionsFile.model_validate_json(raw)
    except ValidationError as e:
        raise LatchkeyMigrationError(f"Permissions file {path} is malformed; cannot migrate it: {e}") from e


def _write_permissions_file(path: Path, config: _PermissionsFile) -> None:
    """Atomically rewrite a permissions file (mode 0600), mirroring the store's write."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(config.model_dump_json(indent=2))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)


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
        for path in _iter_host_permission_files(plugin_data_dir):
            config = _read_permissions_file(path)
            transformed = transform(config)
            if transformed != config:
                logger.debug("Migrating permissions file {} for data-format change", path)
                _write_permissions_file(path, transformed)
