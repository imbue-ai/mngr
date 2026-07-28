"""Data-format migration 3: add the shared additional-services schemas ``include``.

Additional (custom) latchkey services -- e.g. ``claude.ai`` -- expose detent
scope/permission schemas minds ships itself, not from detent's builtin catalog.
Rather than inlining those schemas into every per-host ``latchkey_permissions.json``
whenever such a scope is granted, minds materializes them once into a single
shared file (``minds_shared_schemas.json``) and has each host file reference it
via detent's ``include`` directive. New host files get the include from the agent
baseline; this migration stamps it into per-host files that predate the change,
so a custom-service grant on an existing host resolves its schema instead of
failing.

``apply_up`` appends the shared-schemas filename to each host file's ``include``
list (idempotently); ``apply_down`` removes it. The include is a *bare relative*
name on purpose -- detent resolves it relative to the directory of the file that
references it, which is the shared file's location on both the desktop (the
opaque-handle directory) and a VPS (``~/.latchkey``).

As with the sibling migration, everything the migration needs -- the
permissions-file model, the read/write, and the per-host file walk -- is a small
frozen copy local to this module rather than an import from
:mod:`imbue.mngr_latchkey.store`, so it keeps performing the exact same rewrite
even if the live store model or on-disk layout later changes.
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

# Frozen snapshot of the on-disk filename + layout this migration depends on.
# Hardcoded (not imported from the live store) so the migration reproduces the
# exact historical transform even if those values later change.
_SHARED_SCHEMAS_FILENAME: Final[str] = "minds_shared_schemas.json"
_HOSTS_DIR_NAME: Final[str] = "hosts"
_PERMISSIONS_FILENAME: Final[str] = "latchkey_permissions.json"


class _PermissionsFile(FrozenModel):
    """Migration-local, frozen view of a permissions file (``rules``/``schemas``/``include``).

    A deliberately independent copy of the parts this migration reads and
    rewrites, so it keeps producing the same historical transform even if
    ``store.LatchkeyPermissionsConfig`` later changes. Unrecognized top-level
    keys are dropped on load, matching the store's own save behavior.
    """

    model_config = ConfigDict(extra="ignore")

    rules: tuple[dict[str, list[str]], ...] = Field(default_factory=tuple)
    schemas: dict[str, JsonValue] = Field(default_factory=dict)
    include: tuple[str, ...] = Field(default_factory=tuple)


# Type of the pure per-file transform each migration direction dispatches to.
_ConfigTransform = Callable[[_PermissionsFile], _PermissionsFile]


@pure
def add_shared_schemas_include(config: _PermissionsFile) -> _PermissionsFile:
    """Append the shared-schemas include if it is not already present (idempotent)."""
    if _SHARED_SCHEMAS_FILENAME in config.include:
        return config
    return _PermissionsFile(
        rules=config.rules,
        schemas=config.schemas,
        include=config.include + (_SHARED_SCHEMAS_FILENAME,),
    )


@pure
def remove_shared_schemas_include(config: _PermissionsFile) -> _PermissionsFile:
    """Remove the shared-schemas include, preserving any other includes (idempotent)."""
    if _SHARED_SCHEMAS_FILENAME not in config.include:
        return config
    return _PermissionsFile(
        rules=config.rules,
        schemas=config.schemas,
        include=tuple(name for name in config.include if name != _SHARED_SCHEMAS_FILENAME),
    )


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
    """Atomically rewrite a permissions file (mode 0600), omitting empty ``schemas``/``include``.

    Mirrors ``store.save_permissions`` so the on-disk shape matches what the live
    writers produce (an empty ``schemas``/``include`` is dropped; ``rules`` is
    always emitted).
    """
    exclude: set[str] = set()
    if not config.schemas:
        exclude.add("schemas")
    if not config.include:
        exclude.add("include")
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(config.model_dump_json(indent=2, exclude=exclude))
    tmp_path.chmod(0o600)
    os.replace(tmp_path, path)


class AddSharedSchemasInclude(DataFormatMigration):
    """Add/remove the shared additional-services schemas ``include`` on every per-host file."""

    # ``latchkey_directory`` / ``latchkey_binary`` are part of the migration
    # interface for steps that must inspect upstream latchkey state; this one
    # only rewrites the plugin's own files, so it ignores them.
    def apply_up(self, plugin_data_dir: Path, latchkey_directory: Path, latchkey_binary: str) -> None:
        del latchkey_directory, latchkey_binary
        self._rewrite_each_host_file(plugin_data_dir, add_shared_schemas_include)

    def apply_down(self, plugin_data_dir: Path, latchkey_directory: Path, latchkey_binary: str) -> None:
        del latchkey_directory, latchkey_binary
        self._rewrite_each_host_file(plugin_data_dir, remove_shared_schemas_include)

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
