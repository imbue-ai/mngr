"""Desktop egress: the device-gated detent grant, and the rules file that routes requests.

A remote machine's gateway can send a service's requests out through the
user's computer instead of making them itself. Two files decide that, and this
module is the single author of the shape of both:

- The **rules file** on the machine (``~/.latchkey/proxyRules.json``) names the
  latchkey services whose requests the machine's gateway routes to the desktop
  gateway. The machine's gateway has already injected the credentials and run
  the per-account permission check by then, and it tells its curl router which
  service the request matched, so the router matches no URLs itself.
- A **device-gated rule** in the host's permissions file makes the desktop
  gateway accept those requests. Latchkey reports no ``customMetadata`` for a
  request it injects nothing into, so detent falls back to the metadata the
  desktop gateway was started with (see
  :mod:`imbue.mngr_latchkey.device_metadata`), and the rule gates on the
  ``deviceId`` in it. The permissions file is shared between every computer the
  user connects to the machine from, so without that gate a rule written on one
  computer would make every other one forward too.

As with per-account grants (:mod:`imbue.mngr_latchkey.account_scopes`), the
rule key is an opaque name and is never parsed: readers inspect the schema
structure. A per-account schema requires ``customMetadata.account`` and a
device-gated one requires ``customMetadata.deviceId`` with no account, so the
two kinds never match the same request and their order in a file is irrelevant.
"""

import json
from collections.abc import Iterable
from collections.abc import Mapping
from typing import Final

from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.account_scopes import ACCOUNT_METADATA_KEY
from imbue.mngr_latchkey.account_scopes import SCHEMA_REFERENCE_PREFIX
from imbue.mngr_latchkey.account_scopes import custom_metadata_const_gate
from imbue.mngr_latchkey.account_scopes import referenced_schema_name
from imbue.mngr_latchkey.custom_services import is_custom_service_name
from imbue.mngr_latchkey.device_metadata import DEVICE_ID_METADATA_KEY
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

_SCOPE_KEY_PREFIX: Final[str] = "desktop-egress"
_SCOPE_KEY_SEPARATOR: Final[str] = ":"

# Detent's built-in permission that matches every request. The machine's
# gateway has already checked the request against the per-account rules, so the
# desktop's rule only decides whether this computer forwards for the scope.
DESKTOP_EGRESS_PERMISSIONS: Final[tuple[str, ...]] = ("any",)


class DesktopEgressError(ValueError):
    """Raised when a desktop egress grant or rules file cannot be given a usable shape.

    A ``ValueError`` subclass rather than a ``LatchkeyError`` for the same
    reason :class:`~imbue.mngr_latchkey.account_scopes.LatchkeyAccountScopeError`
    is one: this module must stay importable without ``core``.
    """


class DesktopEgressGrant(FrozenModel):
    """One rule of a permissions file that lets one computer forward the requests of one scope."""

    rule_key: str = Field(
        description=(
            "The rule's key in the file, i.e. the name of its generated schema. Opaque: pass it "
            "back to the gateway to rewrite or delete the rule, but never parse it."
        ),
    )
    scope: str = Field(description="Base detent scope the generated schema composes (e.g. ``slack-api``).")
    device_id: str = Field(description="Id of the computer whose desktop gateway the rule applies on.")


@pure
def desktop_egress_scope_key(scope: str, device_id: str) -> str:
    """Return the rule key (and generated schema name) that lets ``device_id`` forward ``scope``.

    The same (scope, device id) pair always maps to the same key, which makes
    re-granting an idempotent overwrite. Distinct pairs never share a key: the
    device id is refused if it contains the separator, so the separator after it
    is unambiguous, and the scope is the last field, so it may contain anything.
    """
    if not device_id or _SCOPE_KEY_SEPARATOR in device_id:
        raise DesktopEgressError(
            f"Refusing to build a desktop egress rule key for device id {device_id!r}: it must be non-empty and "
            f"must not contain {_SCOPE_KEY_SEPARATOR!r}, or two different rules could share one key."
        )
    return _SCOPE_KEY_SEPARATOR.join((_SCOPE_KEY_PREFIX, device_id, scope))


@pure
def build_desktop_egress_scope_schema(scope: str, device_id: str) -> dict[str, JsonValue]:
    """Build the generated schema backing :func:`desktop_egress_scope_key`.

    The schema intersects the named base ``scope`` with an exact match on the
    device id the desktop gateway reports. The base scope must exist by the
    time the file is evaluated: detent fails the *entire* permission check when
    a referenced schema is unknown.
    """
    return {
        "allOf": [
            {"$ref": f"{SCHEMA_REFERENCE_PREFIX}{scope}"},
            {
                "properties": {
                    "customMetadata": {
                        "type": "object",
                        "properties": {
                            DEVICE_ID_METADATA_KEY: {"const": device_id},
                            # Latchkey reports the account whose credentials it
                            # injects, and it injects none into a forwarded
                            # request. Stated here so the rule cannot match an
                            # injecting request whatever metadata it carries.
                            ACCOUNT_METADATA_KEY: {"type": "null"},
                        },
                        "required": [DEVICE_ID_METADATA_KEY],
                    },
                },
                "required": ["customMetadata"],
            },
        ],
    }


@pure
def build_desktop_egress_grant(
    scope: str,
    device_id: str,
    # The definition of ``scope`` itself. ``None`` leaves the reference to
    # resolve as a detent builtin; a custom service's scope is not one, so it
    # is required for those (read it off the catalog entry's ``scope_schema``).
    base_scope_schema: Mapping[str, JsonValue] | None,
) -> tuple[str, tuple[str, ...], dict[str, JsonValue]]:
    """Assemble everything needed to write one desktop egress grant.

    Returns ``(rule_key, permissions, schemas)``, the same triple
    :func:`~imbue.mngr_latchkey.account_scopes.build_account_grant` returns, to
    hand to the gateway's ``permissions`` extension.
    """
    if base_scope_schema is None and is_custom_service_name(scope):
        raise DesktopEgressError(
            f"Refusing to build a desktop egress grant for custom scope {scope!r} without its scope schema: the "
            "rule would reference a definition the target file need not contain, which fails every permission "
            "check on that host. Pass the catalog entry's scope_schema.",
        )
    rule_key = desktop_egress_scope_key(scope, device_id)
    schemas: dict[str, JsonValue] = {rule_key: build_desktop_egress_scope_schema(scope, device_id)}
    if base_scope_schema is not None:
        schemas[scope] = dict(base_scope_schema)
    return rule_key, DESKTOP_EGRESS_PERMISSIONS, schemas


@pure
def _resolve_desktop_egress_scope(schema: JsonValue | None) -> tuple[str, str] | None:
    """Recover ``(scope, device_id)`` from a generated desktop egress schema, or ``None`` for anything else."""
    members = schema.get("allOf") if isinstance(schema, dict) else None
    if not isinstance(members, list):
        return None
    scopes = [name for name in (referenced_schema_name(member) for member in members) if name is not None]
    device_ids = [
        device_id
        for device_id in (custom_metadata_const_gate(member, DEVICE_ID_METADATA_KEY) for member in members)
        if device_id is not None
    ]
    is_account_gated = any(custom_metadata_const_gate(member, ACCOUNT_METADATA_KEY) is not None for member in members)
    # Exactly one of each and no account: anything else is not the shape we
    # generate, and guessing would risk reporting a rule as something it is not.
    if len(scopes) != 1 or len(device_ids) != 1 or is_account_gated:
        return None
    return scopes[0], device_ids[0]


@pure
def list_desktop_egress_grants(config: LatchkeyPermissionsConfig) -> tuple[DesktopEgressGrant, ...]:
    """Return every desktop egress grant in ``config``, for every device, in file order."""
    grants: list[DesktopEgressGrant] = []
    for rule in config.rules:
        if len(rule) != 1:
            continue
        rule_key = next(iter(rule))
        # The key is not parsed, but it is checked: a rule of the generated
        # shape under some other name is not one this module wrote.
        if not rule_key.startswith(f"{_SCOPE_KEY_PREFIX}{_SCOPE_KEY_SEPARATOR}"):
            continue
        resolved = _resolve_desktop_egress_scope(config.schemas.get(rule_key))
        if resolved is None:
            continue
        scope, device_id = resolved
        grants.append(DesktopEgressGrant(rule_key=rule_key, scope=scope, device_id=device_id))
    return tuple(grants)


@pure
def build_desktop_egress_rules(service_names: Iterable[str]) -> dict[str, bool]:
    """Build the rules that route every one of the latchkey services ``service_names`` through the desktop.

    The values are booleans rather than device ids because a machine can reach
    only one desktop gateway (its reverse tunnel binds a single port).
    """
    return {service_name: True for service_name in sorted(set(service_names))}


@pure
def serialize_desktop_egress_rules(rules: Mapping[str, bool]) -> str:
    return json.dumps(dict(rules), indent=2, sort_keys=True) + "\n"


def _refuse_non_json_constant(constant_name: str) -> JsonValue:
    raise DesktopEgressError(
        f"Desktop egress rules must be strict JSON, which has no {constant_name}: the machine's router could not "
        "parse the file."
    )


@pure
def parse_desktop_egress_rules(text: str) -> dict[str, JsonValue]:
    """Parse the text of a rules file, refusing anything the machine's router would not read as rules.

    Only the outer shape is checked. A value may be any JSON value, because the
    router treats every truthy one as on (see :func:`is_service_routed`).
    """
    try:
        parsed = json.loads(text, parse_constant=_refuse_non_json_constant)
    except json.JSONDecodeError as e:
        raise DesktopEgressError(f"Desktop egress rules are not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise DesktopEgressError(
            f"Desktop egress rules must be one JSON object keyed by latchkey service name, not {type(parsed).__name__}."
        )
    return parsed


@pure
def is_service_routed(rules: Mapping[str, JsonValue], service_name: str) -> bool:
    """Whether the machine's router sends the requests latchkey matched to ``service_name`` through the desktop.

    The router tests the value's truthiness in the JavaScript sense, which
    differs from Python's: an empty list or object is on there.
    """
    value = rules.get(service_name)
    if value is None:
        return False
    elif isinstance(value, bool):
        return value
    elif isinstance(value, (int, float)):
        return value != 0
    elif isinstance(value, str):
        return value != ""
    else:
        return True
