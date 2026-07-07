"""Sequences the plugin's data-format migrations against the recorded version.

The plugin stamps the current on-disk data-format version into a
``data-format-version`` file at the root of ``Latchkey.plugin_data_dir``. On every
:meth:`Latchkey.initialize`, :func:`run_data_format_migrations` compares that
recorded version against :data:`CURRENT_DATA_FORMAT_VERSION` (the highest version
the installed code understands) and, if they differ, applies the intervening
:class:`DataFormatMigration` steps in the appropriate direction -- ``apply_up`` to
move a stale-but-older store forward after an upgrade, ``apply_down`` to move a
newer store back after a downgrade -- then rewrites the version file.

A fresh install (no version file, no host data yet) is stamped straight to the
current version: the up-migrations are no-ops against an empty store, so nothing
is rewritten and future migrations get an accurate baseline.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from loguru import logger

from imbue.mngr_latchkey.migrations.fold_workspace_scope_into_latchkey_self import FoldWorkspaceScopeIntoLatchkeySelf
from imbue.mngr_latchkey.migrations.interface import DataFormatMigration
from imbue.mngr_latchkey.migrations.interface import LatchkeyMigrationError

# The ordered, consecutive-from-1 list of migrations the installed code knows how
# to apply. Append new migrations here (never renumber or reorder existing ones):
# each new entry's ``version`` must be exactly one greater than the last.
_MIGRATIONS: Final[tuple[DataFormatMigration, ...]] = (FoldWorkspaceScopeIntoLatchkeySelf(version=1),)

# The data-format version the installed code targets: the highest known migration
# version (or 0 when there are none). Stamped into the version file after any
# migration run.
CURRENT_DATA_FORMAT_VERSION: Final[int] = max((migration.version for migration in _MIGRATIONS), default=0)

# Name of the file at the root of ``plugin_data_dir`` holding the integer version.
_DATA_FORMAT_VERSION_FILENAME: Final[str] = "data-format-version"


def _data_format_version_path(plugin_data_dir: Path) -> Path:
    return plugin_data_dir / _DATA_FORMAT_VERSION_FILENAME


def read_data_format_version(plugin_data_dir: Path) -> int:
    """Return the recorded on-disk data-format version, or 0 if it has never been stamped."""
    path = _data_format_version_path(plugin_data_dir)
    if not path.is_file():
        return 0
    try:
        raw = path.read_text().strip()
    except OSError as e:
        raise LatchkeyMigrationError(f"Failed to read data-format version file {path}: {e}") from e
    try:
        return int(raw)
    except ValueError as e:
        raise LatchkeyMigrationError(f"Data-format version file {path} does not contain an integer: {raw!r}") from e


def _write_data_format_version(plugin_data_dir: Path, version: int) -> None:
    """Atomically stamp ``version`` into the data-format version file."""
    plugin_data_dir.mkdir(parents=True, exist_ok=True)
    path = _data_format_version_path(plugin_data_dir)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(f"{version}\n")
    tmp_path.replace(path)
    logger.debug("Stamped mngr_latchkey data-format version {} at {}", version, path)


def _validate_migration_sequence(migrations: Sequence[DataFormatMigration]) -> None:
    """Ensure ``migrations`` are numbered consecutively from 1."""
    for index, migration in enumerate(migrations):
        expected_version = index + 1
        if migration.version != expected_version:
            raise LatchkeyMigrationError(
                f"Migration registry is not consecutive from 1: entry {index} has version "
                f"{migration.version}, expected {expected_version}."
            )


def _sequence_migrations(
    plugin_data_dir: Path,
    migrations: Sequence[DataFormatMigration],
    target_version: int,
) -> None:
    """Apply/revert ``migrations`` to move ``plugin_data_dir`` to ``target_version``, then stamp it.

    Cheap in the steady state: when the recorded version already matches the
    target this reads one small file and returns without touching anything else.
    """
    _validate_migration_sequence(migrations)
    recorded_version = read_data_format_version(plugin_data_dir)
    if recorded_version == target_version:
        return
    if recorded_version > target_version:
        # Downgrade: the store was written by a newer build. Revert each migration
        # above the target, highest first.
        for migration in sorted(migrations, key=lambda m: m.version, reverse=True):
            if target_version < migration.version <= recorded_version:
                logger.debug("Reverting mngr_latchkey data-format migration to version {}", migration.version - 1)
                migration.apply_down(plugin_data_dir)
    else:
        # Upgrade: apply each migration above the recorded version, lowest first.
        for migration in sorted(migrations, key=lambda m: m.version):
            if recorded_version < migration.version <= target_version:
                logger.debug("Applying mngr_latchkey data-format migration to version {}", migration.version)
                migration.apply_up(plugin_data_dir)
    _write_data_format_version(plugin_data_dir, target_version)


def run_data_format_migrations(plugin_data_dir: Path) -> None:
    """Bring ``plugin_data_dir`` to :data:`CURRENT_DATA_FORMAT_VERSION`, applying migrations as needed."""
    _sequence_migrations(plugin_data_dir, _MIGRATIONS, CURRENT_DATA_FORMAT_VERSION)
