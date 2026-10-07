from pathlib import Path
from typing import Final

from pydantic import JsonValue

from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.migrations.device_scoped_file_sharing import DeviceScopedFileSharingMigration
from imbue.mngr_latchkey.migrations.runner import CURRENT_PERMISSIONS_FORMAT_VERSION
from imbue.mngr_latchkey.migrations.runner import migrate_permissions
from imbue.mngr_latchkey.primitives import PermissionsFormatVersion
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import read_permissions_format_version
from imbue.mngr_latchkey.testing import MIGRATING_DEVICE_ID
from imbue.mngr_latchkey.testing import MIGRATION_CONTEXT
from imbue.mngr_latchkey.testing import write_raw_host_permissions

_MIGRATION: Final[DeviceScopedFileSharingMigration] = DeviceScopedFileSharingMigration(
    version=PermissionsFormatVersion(1)
)
_BASELINE_PERMISSION: Final[str] = "minds-api-proxy-call-agent-4471"
_VERB_PERMISSION: Final[str] = "minds-workspaces-read"


def _grant_schema(pattern: str) -> dict[str, JsonValue]:
    return {
        "properties": {"method": {"enum": ["GET"]}, "path": {"type": "string", "pattern": pattern}},
        "required": ["method", "path"],
    }


def test_a_grant_from_before_devices_gains_a_twin_for_the_migrating_desktop() -> None:
    legacy_name = "minds-file-server-read-/Users/kim/notes"
    legacy_schema = _grant_schema("^/minds-api-proxy/api/v1/files/Users/kim/notes(/.*)?$")
    legacy_policy = LatchkeyPermissionsConfig(
        rules=({"latchkey-self": [_BASELINE_PERMISSION, legacy_name, _VERB_PERMISSION]},),
        schemas={legacy_name: legacy_schema, _VERB_PERMISSION: {"properties": {}}},
    )

    migrated = _MIGRATION.apply(legacy_policy, MIGRATION_CONTEXT)

    scoped_name = f"minds-file-server-read-{MIGRATING_DEVICE_ID}:/Users/kim/notes"
    assert migrated.rules == ({"latchkey-self": [_BASELINE_PERMISSION, legacy_name, scoped_name, _VERB_PERMISSION]},)
    assert migrated.schemas == {
        legacy_name: legacy_schema,
        scoped_name: _grant_schema(f"^/minds-api-proxy/api/v1/files/{MIGRATING_DEVICE_ID}/Users/kim/notes(/.*)?$"),
        _VERB_PERMISSION: {"properties": {}},
    }


def test_a_revoked_grant_from_before_devices_gains_a_twin_that_is_revoked_too() -> None:
    """Revoking leaves the schema behind so the grant can be restored; the twin is as restorable, and as revoked."""
    legacy_name = "minds-file-server-write-/tmp/x"
    policy = LatchkeyPermissionsConfig(
        rules=({"latchkey-self": [_BASELINE_PERMISSION]},),
        schemas={legacy_name: _grant_schema("^/minds-api-proxy/api/v1/files/tmp/x(/.*)?$")},
    )

    migrated = _MIGRATION.apply(policy, MIGRATION_CONTEXT)

    assert migrated.rules == policy.rules
    assert set(migrated.schemas) == {legacy_name, f"minds-file-server-write-{MIGRATING_DEVICE_ID}:/tmp/x"}


def test_a_policy_already_in_the_new_shape_is_left_alone() -> None:
    scoped_name = "minds-file-server-write-host-elsewhere:/Users/kim/notes"
    policy = LatchkeyPermissionsConfig(
        rules=({"latchkey-self": [scoped_name]},),
        schemas={scoped_name: _grant_schema("^/minds-api-proxy/api/v1/files/host-elsewhere/Users/kim/notes(/.*)?$")},
    )

    assert _MIGRATION.apply(policy, MIGRATION_CONTEXT) is policy
    migrated_once = _MIGRATION.apply(
        LatchkeyPermissionsConfig(
            rules=({"latchkey-self": ["minds-file-server-read-/tmp/x"]},),
            schemas={"minds-file-server-read-/tmp/x": _grant_schema("^/minds-api-proxy/api/v1/files/tmp/x(/.*)?$")},
        ),
        MIGRATION_CONTEXT,
    )
    assert _MIGRATION.apply(migrated_once, MIGRATION_CONTEXT) is migrated_once


def test_a_grant_whose_schema_the_gateway_did_not_write_is_left_as_it_is() -> None:
    """Such a name matches no URL either way; rewriting a schema of an unknown shape could only make things worse."""
    legacy_name = "minds-file-server-read-/tmp/hand-edited"
    policy = LatchkeyPermissionsConfig(
        rules=({"latchkey-self": [legacy_name]},),
        schemas={legacy_name: {"type": "object"}},
    )

    assert _MIGRATION.apply(policy, MIGRATION_CONTEXT) is policy


def test_a_legacy_grant_whose_twin_was_revoked_since_is_not_granted_again() -> None:
    """A second run, after a failure between the policy and its stamp, must not undo what the user did in between."""
    legacy_name = "minds-file-server-read-/tmp/x"
    scoped_name = f"minds-file-server-read-{MIGRATING_DEVICE_ID}:/tmp/x"
    policy = LatchkeyPermissionsConfig(
        rules=({"latchkey-self": [_BASELINE_PERMISSION]},),
        schemas={
            legacy_name: _grant_schema("^/minds-api-proxy/api/v1/files/tmp/x(/.*)?$"),
            scoped_name: _grant_schema(f"^/minds-api-proxy/api/v1/files/{MIGRATING_DEVICE_ID}/tmp/x(/.*)?$"),
        },
    )

    assert _MIGRATION.apply(policy, MIGRATION_CONTEXT) is policy


def test_the_build_migrates_a_host_from_before_devices_to_the_current_format(tmp_path: Path) -> None:
    """The migration is registered: an unstamped policy leaves the runner with its device-scoped twins, and stamped."""
    host_id = HostId.generate()
    legacy_name = "minds-file-server-write-/tmp/shared"
    write_raw_host_permissions(
        tmp_path,
        host_id,
        LatchkeyPermissionsConfig(
            rules=({"latchkey-self": [legacy_name]},),
            schemas={legacy_name: _grant_schema("^/minds-api-proxy/api/v1/files/tmp/shared(/.*)?$")},
        ).model_dump_json(),
    )

    assert migrate_permissions(tmp_path, host_id, MIGRATION_CONTEXT) is True

    migrated = load_permissions(permissions_path_for_host(tmp_path, host_id))
    assert migrated.rules == (
        {"latchkey-self": [legacy_name, f"minds-file-server-write-{MIGRATING_DEVICE_ID}:/tmp/shared"]},
    )
    assert read_permissions_format_version(tmp_path, host_id) == CURRENT_PERMISSIONS_FORMAT_VERSION
