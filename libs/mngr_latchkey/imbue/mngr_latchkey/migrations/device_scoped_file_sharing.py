"""Format version 1: a file-sharing grant names the desktop whose file it shares.

Before it, a grant was ``minds-file-server-<access>-<path>`` and matched the
file server's URL ``/minds-api-proxy/api/v1/files<path>``. Each desktop now
serves its files under its own device id, where a grant is
``minds-file-server-<access>-<device id>:<path>`` and matches
``/minds-api-proxy/api/v1/files/<device id><path>`` (see
:mod:`imbue.mngr_latchkey.file_sharing`). A policy that predates the change was
granted by the only desktop there was, so each of its grants gains a twin
naming the desktop migrating it. The grant itself stays where it was: an agent
that reaches shared files through the device-less URL goes on reaching them that
way.

The URL pattern is rewritten rather than rebuilt: the extension derived it from
the path with the browser's URL normalization, which has no exact counterpart
here, and a device id is left unchanged by that normalization, so inserting it
after the prefix is exactly what the extension computes for the same grant
today.
"""

from collections.abc import Mapping
from collections.abc import Sequence
from typing import Final

from loguru import logger
from pydantic import JsonValue

from imbue.imbue_common.model_update import to_update
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.file_sharing import FileSharingGrant
from imbue.mngr_latchkey.file_sharing import file_sharing_permission_name
from imbue.mngr_latchkey.file_sharing import parse_legacy_file_sharing_permission
from imbue.mngr_latchkey.migrations.interface import PermissionsMigration
from imbue.mngr_latchkey.migrations.interface import PermissionsMigrationContext
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

_URL_PATTERN_PREFIX: Final[str] = "^/minds-api-proxy/api/v1/files/"

# What ``escapeForRegex`` in ``permission_requests.mjs`` escapes. Python's
# ``re.escape`` escapes more (``-`` among others), which a JavaScript pattern
# compiled with the ``u`` flag rejects.
_JAVASCRIPT_REGEX_SPECIAL_CHARACTERS: Final[frozenset[str]] = frozenset(".*+?^${}()|[]\\")


# CLEANUP: once no supported agent reaches shared files through the device-less URL, add a
# migration that deletes the legacy grants this one leaves beside their twins.
class DeviceScopedFileSharingMigration(PermissionsMigration):
    """Gives every file-sharing grant that names no desktop a twin naming the desktop migrating the policy."""

    def apply(
        self, permissions: LatchkeyPermissionsConfig, context: PermissionsMigrationContext
    ) -> LatchkeyPermissionsConfig:
        scoped_name_by_legacy_name: dict[str, str] = {}
        schemas: dict[str, JsonValue] = {}
        for name, schema in permissions.schemas.items():
            schemas[name] = schema
            legacy_grant = parse_legacy_file_sharing_permission(name)
            if legacy_grant is None:
                continue
            scoped_schema = _scope_url_pattern_to_device(schema, context.device_id)
            if scoped_schema is None:
                logger.warning(
                    "Giving the file-sharing permission {} no twin for this desktop: its schema is not one the "
                    "gateway wrote",
                    name,
                )
                continue
            scoped_name = file_sharing_permission_name(
                FileSharingGrant(access=legacy_grant.access, device_id=context.device_id, path=legacy_grant.path)
            )
            scoped_name_by_legacy_name[name] = scoped_name
            if scoped_name not in permissions.schemas:
                schemas[scoped_name] = scoped_schema
        rules = tuple(_add_scoped_twins(rule, scoped_name_by_legacy_name) for rule in permissions.rules)
        if schemas == permissions.schemas and rules == permissions.rules:
            return permissions
        return permissions.model_copy_update(
            to_update(permissions.field_ref().schemas, schemas),
            to_update(permissions.field_ref().rules, rules),
        )


@pure
def _scope_url_pattern_to_device(schema: JsonValue, device_id: DesktopDeviceId) -> JsonValue | None:
    """``schema`` with its URL pattern moved under ``device_id``, or ``None`` when it has no pattern the gateway wrote."""
    if not isinstance(schema, dict):
        return None
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return None
    path_schema = properties.get("path")
    if not isinstance(path_schema, dict):
        return None
    pattern = path_schema.get("pattern")
    if not isinstance(pattern, str) or not pattern.startswith(_URL_PATTERN_PREFIX):
        return None
    scoped_pattern = (
        f"{_URL_PATTERN_PREFIX}{_escape_for_javascript_regex(device_id)}/{pattern[len(_URL_PATTERN_PREFIX) :]}"
    )
    return {**schema, "properties": {**properties, "path": {**path_schema, "pattern": scoped_pattern}}}


@pure
def _escape_for_javascript_regex(literal: str) -> str:
    return "".join(
        f"\\{character}" if character in _JAVASCRIPT_REGEX_SPECIAL_CHARACTERS else character for character in literal
    )


@pure
def _add_scoped_twins(
    rule: Mapping[str, Sequence[str]], scoped_name_by_legacy_name: Mapping[str, str]
) -> dict[str, list[str]]:
    """``rule`` with each legacy permission followed by its device-scoped twin, unless the rule already carries it."""
    twinned_rule: dict[str, list[str]] = {}
    for scope, permission_names in rule.items():
        twinned: list[str] = []
        for permission_name in permission_names:
            twinned.append(permission_name)
            scoped_name = scoped_name_by_legacy_name.get(permission_name)
            if scoped_name is not None and scoped_name not in permission_names and scoped_name not in twinned:
                twinned.append(scoped_name)
        twinned_rule[scope] = twinned
    return twinned_rule
