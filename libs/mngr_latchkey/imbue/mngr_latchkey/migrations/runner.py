"""Brings one host's policy to the format this build reads, one migration at a time.

Each host's directory carries a stamp (``permissions-format-version``, see
:func:`~imbue.mngr_latchkey.store.read_permissions_format_version`) naming the
format its policy is written in; a directory without one is at version 0.
:func:`migrate_permissions` compares that stamp against the version the build's
migrations (:data:`PERMISSIONS_MIGRATIONS`) end in and applies the ones above
it, in order, re-stamping after each -- so a migration that fails leaves the
stamp at the last step that completed, and the next run picks up there.

Migrations are per host because that is where the source of truth lives. A
host with a machine of its own (a remote host's VPS) keeps the same stamp
beside the policy on that machine, and both are adopted from it before anything
runs here; the migrated policy is then carried back to the machine, stamp and
all (see :func:`~imbue.mngr_latchkey.remote.credentials.migrate_permissions_and_push`).
A host with no machine of its own has its only copy here, and is migrated in
place before the gateway that reads it starts (``mngr latchkey forward``).

A policy stamped newer than this build knows is refused rather than read: a
build that does not know a format cannot edit a policy in it without corrupting
it, and only a newer build can bring it back.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Final

from imbue.imbue_common.logging import log_span
from imbue.imbue_common.pure import pure
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.device_scoped_file_sharing import DeviceScopedFileSharingMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsFormatNewerError
from imbue.mngr_latchkey.migrations.interface import PermissionsMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationError
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import LatchkeyStoreError
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import read_permissions_format_version
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.store import write_permissions_format_version

# The migrations this build knows, in the order they apply. Append new ones
# here, never renumber or reorder existing ones: each entry's version is one
# more than the last.
PERMISSIONS_MIGRATIONS: Final[tuple[PermissionsMigration, ...]] = (
    DeviceScopedFileSharingMigration(version=PermissionsFormatVersion(1)),
)


@pure
def latest_permissions_format_version(migrations: Sequence[PermissionsMigration]) -> PermissionsFormatVersion:
    """The format version a policy is in once every one of ``migrations`` has run; 0 when there are none."""
    return PermissionsFormatVersion(max((migration.version for migration in migrations), default=0))


# The format version this build writes a policy in: what a policy this build
# creates is stamped with.
CURRENT_PERMISSIONS_FORMAT_VERSION: Final[PermissionsFormatVersion] = latest_permissions_format_version(
    PERMISSIONS_MIGRATIONS
)


def migrate_permissions(
    data_dir: Path,
    host_id: HostId,
    context: PermissionsMigrationContext,
    migrations: Sequence[PermissionsMigration] = PERMISSIONS_MIGRATIONS,
    # whether the policy was rewritten, and so has to reach the host's machine when it has one
) -> bool:
    """Bring this computer's copy of ``host_id``'s policy to the format ``migrations`` end in.

    A host with no policy here has nothing to migrate, and is left unstamped:
    its stamp arrives with the policy when one is adopted from its machine, or
    is written when a policy is first created for it.

    Raises:
        PermissionsFormatNewerError: when the policy is stamped newer than
            ``migrations`` know, so only a newer build can read it.
        PermissionsMigrationError: when the policy or its stamp cannot be read
            or written, ``migrations`` are not numbered consecutively from 1,
            or a migration fails.
    """
    _validate_migration_sequence(migrations)
    target_version = latest_permissions_format_version(migrations)
    permissions_path = permissions_path_for_host(data_dir, host_id)
    if not permissions_path.is_file():
        return False
    try:
        recorded_version = read_permissions_format_version(data_dir, host_id)
    except LatchkeyStoreError as e:
        raise PermissionsMigrationError(f"Cannot migrate the permissions of host {host_id}: {e}") from e
    if recorded_version > target_version:
        raise PermissionsFormatNewerError(
            f"The permissions of host {host_id} are in format version {recorded_version}, which this build does not "
            f"know (it reads up to version {target_version}); a newer build wrote them, and only one can read them"
        )
    if recorded_version == target_version:
        return False
    try:
        permissions = load_permissions(permissions_path)
    except LatchkeyStoreError as e:
        raise PermissionsMigrationError(f"Cannot migrate the permissions of host {host_id}: {e}") from e
    for migration in migrations:
        if migration.version <= recorded_version:
            continue
        with log_span("Migrating the permissions of host {} to format version {}", host_id, migration.version):
            permissions = migration.apply(permissions, context)
        _save_migrated_permissions(data_dir, host_id, permissions, migration.version)
    return True


def _save_migrated_permissions(
    data_dir: Path, host_id: HostId, permissions: LatchkeyPermissionsConfig, version: PermissionsFormatVersion
) -> None:
    """Write the policy one migration produced, then the stamp that says which version it is in.

    Raises:
        PermissionsMigrationError: when either cannot be written.
    """
    try:
        save_permissions(permissions_path_for_host(data_dir, host_id), permissions)
        write_permissions_format_version(data_dir, host_id, version)
    except LatchkeyStoreError as e:
        raise PermissionsMigrationError(
            f"Migrated the permissions of host {host_id} to format version {version} but could not save them: {e}"
        ) from e


@pure
def _validate_migration_sequence(migrations: Sequence[PermissionsMigration]) -> None:
    """Raises :class:`PermissionsMigrationError` unless ``migrations`` are numbered consecutively from 1."""
    for index, migration in enumerate(migrations):
        expected_version = index + 1
        if migration.version != expected_version:
            raise PermissionsMigrationError(
                f"The permissions migrations are not numbered consecutively from 1: entry {index} has version "
                f"{migration.version}, expected {expected_version}"
            )
