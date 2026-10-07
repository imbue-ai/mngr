"""Desktop egress: where a service's requests leave from, and the grants that let a desktop send them.

A remote machine's gateway can send a service's requests out through one of the
user's computers instead of making them itself. Two files decide that, and this
module is the single author of the shape of both:

- The **rules file** on the machine (``~/.latchkey/proxyRules.json``) gives each
  latchkey service a **route**: the places its requests may leave from, in the
  order the machine's curl router tries them. A place is one desktop, named by
  its device id, or the machine itself (:data:`SELF_HOP`). A service the file
  does not name leaves from the machine. The machine's gateway has already
  injected the credentials and run the per-account permission check by then,
  and it tells the router which service the request matched, so the router
  matches no URLs itself.
- A **device-gated rule** in the host's permissions file makes one desktop's
  gateway accept those requests. Latchkey reports no ``customMetadata`` for a
  request it injects nothing into, so detent falls back to the metadata the
  desktop gateway was started with (see
  :mod:`imbue.mngr_latchkey.device_metadata`), and the rule gates on the
  ``deviceId`` in it. The permissions file is shared between every computer the
  user connects to the machine from, so without that gate a rule for one
  desktop would make every other one forward too. That is also why a route
  names its desktops one by one: there is no rule for a desktop nobody named.

The rules file is the record of what the user chose. The rules in the
permissions file follow from it (:func:`desktop_egress_forwarders`): every
writer of a route writes the rules its desktops need in the same change.

As with per-account grants (:mod:`imbue.mngr_latchkey.account_scopes`), the
rule key is an opaque name and is never parsed: readers inspect the schema
structure. A per-account schema requires ``customMetadata.account`` and a
device-gated one requires ``customMetadata.deviceId`` with no account, so the
two kinds never match the same request and their order in a file is irrelevant.
"""

import json
from collections.abc import Mapping
from collections.abc import Sequence
from enum import auto
from typing import Final
from typing import Self

from pydantic import Field
from pydantic import JsonValue
from pydantic import model_validator

from imbue.imbue_common.enums import UpperCaseStrEnum
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.primitives import InvalidPrimitiveValueError
from imbue.imbue_common.pure import pure
from imbue.mngr_latchkey.account_scopes import ACCOUNT_METADATA_KEY
from imbue.mngr_latchkey.account_scopes import SCHEMA_REFERENCE_PREFIX
from imbue.mngr_latchkey.account_scopes import custom_metadata_const_gate
from imbue.mngr_latchkey.account_scopes import referenced_schema_name
from imbue.mngr_latchkey.custom_services import is_custom_service_name
from imbue.mngr_latchkey.device_metadata import DEVICE_ID_METADATA_KEY
from imbue.mngr_latchkey.devices import DesktopDeviceId
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

_SCOPE_KEY_PREFIX: Final[str] = "desktop-egress"
_SCOPE_KEY_SEPARATOR: Final[str] = ":"

# Detent's built-in permission that matches every request. The machine's
# gateway has already checked the request against the per-account rules, so the
# desktop's rule only decides whether this computer forwards for the scope.
DESKTOP_EGRESS_PERMISSIONS: Final[tuple[str, ...]] = ("any",)

# The one hop of a route that is not a desktop's device id, spelled as the
# machine's curl router reads it.
SELF_HOP: Final[str] = "self"


class DesktopEgressError(ValueError):
    """Raised when a desktop egress route, grant or rules file cannot be given a usable shape.

    A ``ValueError`` subclass rather than a ``LatchkeyError`` for the same
    reason :class:`~imbue.mngr_latchkey.account_scopes.LatchkeyAccountScopeError`
    is one: this module must stay importable without ``core``.
    """


@pure
def _is_device_id(hop: str) -> bool:
    try:
        # The primitive strips surrounding whitespace, and a hop is stored as written.
        return DesktopDeviceId(hop) == hop
    except InvalidPrimitiveValueError:
        return False


@pure
def _describe_invalid_hops(hops: Sequence[str]) -> str | None:
    """Why ``hops`` are not a route, or ``None`` when they are one."""
    if not hops:
        return f"a route must name at least one place for requests to leave from; use [{SELF_HOP!r}] for none."
    if len(set(hops)) != len(hops):
        return f"a route must not name the same place twice: {list(hops)!r}."
    if SELF_HOP in hops[:-1]:
        return (
            f"{SELF_HOP!r} must come last in a route: the machine itself is always there to send a request, so "
            f"nothing after it would ever be tried: {list(hops)!r}."
        )
    for hop in hops:
        if hop != SELF_HOP and not _is_device_id(hop):
            return f"{hop!r} is not {SELF_HOP!r} or a device id."
    return None


@pure
def _require_hops_form_a_route(hops: Sequence[str]) -> None:
    """Raise :class:`DesktopEgressError` unless ``hops`` are a route."""
    problem = _describe_invalid_hops(hops)
    if problem is not None:
        raise DesktopEgressError(f"Not a desktop egress route: {problem}")


class DesktopEgressRoute(FrozenModel):
    """Where one service's requests leave from: the places to try, in order."""

    hops: tuple[str, ...] = Field(
        description=(
            f"Each hop is a desktop's device id, or {SELF_HOP!r} for the machine itself, which is only ever "
            "the last hop."
        ),
    )

    @model_validator(mode="after")
    def _hops_form_a_route(self) -> Self:
        _require_hops_form_a_route(self.hops)
        return self


# The route of a service the rules file does not name: the machine sends its requests itself.
SELF_ONLY_DESKTOP_EGRESS_ROUTE: Final[DesktopEgressRoute] = DesktopEgressRoute(hops=(SELF_HOP,))


class DesktopEgressMode(UpperCaseStrEnum):
    """What a plain on/off switch on one computer can say about a route."""

    # The machine sends the service's requests itself.
    OFF = auto()
    # They go out through this computer, and nowhere else.
    ON = auto()
    # Any other route: one through another desktop, through several, or falling back to the machine.
    CUSTOM = auto()


@pure
def desktop_egress_mode_for_route(
    route: DesktopEgressRoute,
    # The computer the switch is drawn on.
    device_id: str,
) -> DesktopEgressMode:
    if route == SELF_ONLY_DESKTOP_EGRESS_ROUTE:
        return DesktopEgressMode.OFF
    elif route.hops == (device_id,):
        return DesktopEgressMode.ON
    else:
        return DesktopEgressMode.CUSTOM


@pure
def build_desktop_egress_route(hops: Sequence[str]) -> DesktopEgressRoute:
    """Build the route that tries ``hops`` in order, refusing hops that do not form one with :class:`DesktopEgressError`."""
    _require_hops_form_a_route(hops)
    return DesktopEgressRoute(hops=tuple(hops))


@pure
def desktop_egress_forwarders(route: DesktopEgressRoute) -> tuple[str, ...]:
    """The device ids of the desktops whose gateways have to accept the requests ``route`` sends them."""
    return tuple(hop for hop in route.hops if hop != SELF_HOP)


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
def desktop_egress_route_from_grants(
    # Every scope of the service.
    service_scopes: Sequence[str],
    # Every desktop egress grant in the host's permissions file, in file order.
    grants: Sequence[DesktopEgressGrant],
) -> DesktopEgressRoute:
    """The route of a service that was turned on before routes existed.

    The rules file then said only that the service's requests went to a
    desktop, and a desktop forwarded them when it held a grant on every scope
    of the service. So the route goes through each such desktop, in the order
    the file names them, and is the machine itself when there is none.
    """
    # CLEANUP: drop this with the boolean branch of the rules reader, once no machine holds a rules
    # file written before routes.
    if not service_scopes:
        return SELF_ONLY_DESKTOP_EGRESS_ROUTE
    device_ids = tuple(dict.fromkeys(grant.device_id for grant in grants))
    granted_pairs = frozenset((grant.device_id, grant.scope) for grant in grants)
    forwarders = tuple(
        device_id
        for device_id in device_ids
        if device_id != SELF_HOP
        and _is_device_id(device_id)
        and all((device_id, scope) in granted_pairs for scope in service_scopes)
    )
    if not forwarders:
        return SELF_ONLY_DESKTOP_EGRESS_ROUTE
    return DesktopEgressRoute(hops=forwarders)


class DesktopEgressRules(FrozenModel):
    """What a rules file says: the route of every service it sends anywhere but the machine."""

    route_by_service_name: dict[str, DesktopEgressRoute] = Field(
        description="The route of each service the file gives one; a service at the machine itself is left out.",
    )
    # CLEANUP: drop this field once no machine holds a rules file written before routes. Such a file
    # is rewritten as routes by the first route change made for its host.
    legacy_enabled_service_names: tuple[str, ...] = Field(
        description=(
            "The services a file written before routes turned on with a bare ``true``. Their routes are not "
            "in the file: they follow from the grants (:func:`desktop_egress_route_from_grants`)."
        ),
    )


@pure
def desktop_egress_route_for_service(
    route_by_service_name: Mapping[str, DesktopEgressRoute], service_name: str
) -> DesktopEgressRoute:
    """The route of ``service_name``: the one the rules give it, or the machine itself when they name none."""
    return route_by_service_name.get(service_name, SELF_ONLY_DESKTOP_EGRESS_ROUTE)


@pure
def serialize_desktop_egress_rules(route_by_service_name: Mapping[str, DesktopEgressRoute]) -> str:
    """Write the rules file that gives each service its route.

    A service whose requests leave from the machine alone is left out: that is
    what the router does with a service the file does not name.
    """
    rules = {
        service_name: list(route.hops)
        for service_name, route in route_by_service_name.items()
        if route != SELF_ONLY_DESKTOP_EGRESS_ROUTE
    }
    return json.dumps(rules, indent=2, sort_keys=True) + "\n"


def _refuse_non_json_constant(constant_name: str) -> JsonValue:
    raise DesktopEgressError(
        f"Desktop egress rules must be strict JSON, which has no {constant_name}: the machine's router could not "
        "parse the file."
    )


@pure
def _parse_desktop_egress_route(service_name: str, value: JsonValue) -> DesktopEgressRoute:
    if not isinstance(value, list) or not all(isinstance(hop, str) for hop in value):
        raise DesktopEgressError(
            f"The desktop egress rule of {service_name!r} must be a list of hops, not {json.dumps(value)}."
        )
    try:
        return build_desktop_egress_route(tuple(str(hop) for hop in value))
    except DesktopEgressError as e:
        raise DesktopEgressError(f"The desktop egress rule of {service_name!r} cannot be used: {e}") from e


@pure
def parse_desktop_egress_rules(text: str) -> DesktopEgressRules:
    """Parse the text of a rules file.

    Raises :class:`DesktopEgressError` for anything that is not a rules file,
    including a rule that is not a route: guessing at one would show the user a
    route the machine's router does not follow.
    """
    try:
        parsed = json.loads(text, parse_constant=_refuse_non_json_constant)
    except json.JSONDecodeError as e:
        raise DesktopEgressError(f"Desktop egress rules are not valid JSON: {e}") from e
    if not isinstance(parsed, dict):
        raise DesktopEgressError(
            f"Desktop egress rules must be one JSON object keyed by latchkey service name, not {type(parsed).__name__}."
        )
    route_by_service_name: dict[str, DesktopEgressRoute] = {}
    legacy_enabled_service_names: list[str] = []
    for service_name, value in parsed.items():
        if isinstance(value, bool):
            if value:
                legacy_enabled_service_names.append(service_name)
            continue
        route = _parse_desktop_egress_route(service_name, value)
        if route != SELF_ONLY_DESKTOP_EGRESS_ROUTE:
            route_by_service_name[service_name] = route
    return DesktopEgressRules(
        route_by_service_name=route_by_service_name,
        legacy_enabled_service_names=tuple(legacy_enabled_service_names),
    )
