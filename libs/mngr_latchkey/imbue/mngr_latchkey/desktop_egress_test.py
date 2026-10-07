import json
from itertools import product

import pytest
from inline_snapshot import snapshot
from pydantic import JsonValue
from pydantic import ValidationError

from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.account_scopes import list_account_grants
from imbue.mngr_latchkey.account_scopes import resolve_account_scope
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.desktop_egress import DESKTOP_EGRESS_PERMISSIONS
from imbue.mngr_latchkey.desktop_egress import DesktopEgressError
from imbue.mngr_latchkey.desktop_egress import DesktopEgressGrant
from imbue.mngr_latchkey.desktop_egress import DesktopEgressMode
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRoute
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRules
from imbue.mngr_latchkey.desktop_egress import SELF_ONLY_DESKTOP_EGRESS_ROUTE
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_grant
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_route
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_scope_schema
from imbue.mngr_latchkey.desktop_egress import desktop_egress_forwarders
from imbue.mngr_latchkey.desktop_egress import desktop_egress_mode_for_route
from imbue.mngr_latchkey.desktop_egress import desktop_egress_route_for_service
from imbue.mngr_latchkey.desktop_egress import desktop_egress_route_from_grants
from imbue.mngr_latchkey.desktop_egress import desktop_egress_scope_key
from imbue.mngr_latchkey.desktop_egress import list_desktop_egress_grants
from imbue.mngr_latchkey.desktop_egress import parse_desktop_egress_rules
from imbue.mngr_latchkey.desktop_egress import serialize_desktop_egress_rules
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.testing import permissions_config_holding_grants

_DEVICE_ID = "device-7c1f0a"
_OTHER_DEVICE_ID = "host-3f9c"


def test_desktop_egress_scope_key_embeds_the_device_id_and_the_scope() -> None:
    assert desktop_egress_scope_key("slack-api", _DEVICE_ID) == snapshot("desktop-egress:device-7c1f0a:slack-api")


def test_desktop_egress_scope_key_is_injective_over_scopes_containing_the_separator() -> None:
    scopes = ("", ":", "a", "a:", ":a", "a:b", "b")
    device_ids = ("a", "b", "ab")
    pairs = list(product(scopes, device_ids))
    keys = {desktop_egress_scope_key(scope, device_id) for scope, device_id in pairs}
    assert len(keys) == len(pairs)


@pytest.mark.parametrize("device_id", ["", "device:7", ":"])
def test_desktop_egress_scope_key_refuses_a_device_id_that_could_make_two_pairs_share_a_key(device_id: str) -> None:
    with pytest.raises(DesktopEgressError, match="device id"):
        desktop_egress_scope_key("slack-api", device_id)


def test_desktop_egress_scope_schema_composes_the_base_scope_with_a_device_id_gate() -> None:
    assert build_desktop_egress_scope_schema("slack-api", _DEVICE_ID) == snapshot(
        {
            "allOf": [
                {"$ref": "#/$defs/slack-api"},
                {
                    "properties": {
                        "customMetadata": {
                            "type": "object",
                            "properties": {"deviceId": {"const": "device-7c1f0a"}, "account": {"type": "null"}},
                            "required": ["deviceId"],
                        }
                    },
                    "required": ["customMetadata"],
                },
            ]
        }
    )


def test_desktop_egress_grant_for_a_shipped_scope_allows_everything_and_defines_only_the_device_gate() -> None:
    rule_key, permissions, schemas = build_desktop_egress_grant("slack-api", _DEVICE_ID, None)
    assert rule_key == desktop_egress_scope_key("slack-api", _DEVICE_ID)
    assert permissions == (WILDCARD_PERMISSION_NAME,)
    assert DESKTOP_EGRESS_PERMISSIONS == (WILDCARD_PERMISSION_NAME,)
    # Detent ships the base, so emitting one here would shadow it.
    assert schemas == {rule_key: build_desktop_egress_scope_schema("slack-api", _DEVICE_ID)}


def test_desktop_egress_grant_for_a_custom_scope_defines_the_scope_it_refers_to() -> None:
    base = build_custom_service_scope_schema("example.com", "https")
    rule_key, _permissions, schemas = build_desktop_egress_grant("custom_example_com", _DEVICE_ID, base)
    assert schemas == {
        rule_key: build_desktop_egress_scope_schema("custom_example_com", _DEVICE_ID),
        "custom_example_com": base,
    }


def test_desktop_egress_grant_for_a_custom_scope_without_its_schema_is_refused() -> None:
    with pytest.raises(DesktopEgressError, match="custom_example_com"):
        build_desktop_egress_grant("custom_example_com", _DEVICE_ID, None)


def test_list_desktop_egress_grants_reports_built_grants_and_ignores_per_account_grants() -> None:
    # A scope containing the separator shows the key is never parsed.
    config = permissions_config_holding_grants(
        build_account_grant("slack-api", "hynek@imbue-ai", ("slack-read-all",)),
        build_desktop_egress_grant("slack-api", _DEVICE_ID, None),
        build_desktop_egress_grant("odd:scope", "other-device", None),
    )
    assert list_desktop_egress_grants(config) == (
        DesktopEgressGrant(
            rule_key=desktop_egress_scope_key("slack-api", _DEVICE_ID), scope="slack-api", device_id=_DEVICE_ID
        ),
        DesktopEgressGrant(
            rule_key=desktop_egress_scope_key("odd:scope", "other-device"), scope="odd:scope", device_id="other-device"
        ),
    )


def test_list_desktop_egress_grants_ignores_a_generated_schema_under_a_key_without_the_prefix() -> None:
    rule_key, permissions, schemas = build_desktop_egress_grant("slack-api", _DEVICE_ID, None)
    renamed_config = LatchkeyPermissionsConfig(
        rules=({"hand-written": list(permissions)},), schemas={"hand-written": schemas[rule_key]}
    )
    assert list_desktop_egress_grants(renamed_config) == ()


def test_list_desktop_egress_grants_ignores_a_schema_gated_on_both_the_account_and_the_device_id() -> None:
    both_gates: JsonValue = {
        "allOf": [
            {"$ref": "#/$defs/slack-api"},
            {
                "properties": {
                    "customMetadata": {
                        "type": "object",
                        "properties": {"account": {"const": "hynek@imbue-ai"}, "deviceId": {"const": _DEVICE_ID}},
                        "required": ["account", "deviceId"],
                    }
                },
                "required": ["customMetadata"],
            },
        ]
    }
    rule_key = desktop_egress_scope_key("slack-api", _DEVICE_ID)
    config = LatchkeyPermissionsConfig(rules=({rule_key: ["any"]},), schemas={rule_key: both_gates})
    assert list_desktop_egress_grants(config) == ()


@pytest.mark.parametrize(
    "schema",
    [
        None,
        "slack-api",
        {"allOf": "not-a-list"},
        # No device gate at all.
        {"allOf": [{"$ref": "#/$defs/slack-api"}]},
        # Two base scopes: not the shape this module writes.
        {
            "allOf": [
                {"$ref": "#/$defs/slack-api"},
                {"$ref": "#/$defs/github-api"},
                {"properties": {"customMetadata": {"properties": {"deviceId": {"const": _DEVICE_ID}}}}},
            ]
        },
    ],
)
def test_list_desktop_egress_grants_ignores_other_schema_shapes(schema: JsonValue) -> None:
    rule_key = desktop_egress_scope_key("slack-api", _DEVICE_ID)
    schemas: dict[str, JsonValue] = {} if schema is None else {rule_key: schema}
    config = LatchkeyPermissionsConfig(rules=({rule_key: ["any"]},), schemas=schemas)
    assert list_desktop_egress_grants(config) == ()


@pytest.mark.parametrize(
    "device_id_gate",
    [
        {"type": "string"},
        {"type": "string", "minLength": 1},
        {"enum": [_DEVICE_ID, "other-device"]},
        {"const": 7},
        {},
    ],
)
def test_list_desktop_egress_grants_ignores_a_device_gate_that_does_not_name_exactly_one_device(
    device_id_gate: dict[str, JsonValue],
) -> None:
    rule_key = desktop_egress_scope_key("slack-api", _DEVICE_ID)
    schema: JsonValue = {
        "allOf": [
            {"$ref": "#/$defs/slack-api"},
            {
                "properties": {
                    "customMetadata": {
                        "type": "object",
                        "properties": {"deviceId": device_id_gate, "account": {"type": "null"}},
                        "required": ["deviceId"],
                    }
                },
                "required": ["customMetadata"],
            },
        ]
    }
    config = LatchkeyPermissionsConfig(rules=({rule_key: ["any"]},), schemas={rule_key: schema})
    assert list_desktop_egress_grants(config) == ()


def test_per_account_readers_ignore_a_desktop_egress_grant() -> None:
    grant = build_desktop_egress_grant("slack-api", _DEVICE_ID, None)
    rule_key, _permissions, schemas = grant
    assert resolve_account_scope(schemas[rule_key]) is None
    assert list_account_grants(permissions_config_holding_grants(grant)) == ()


@pytest.mark.parametrize(
    "hops",
    [
        ("self",),
        (_DEVICE_ID,),
        (_DEVICE_ID, "self"),
        (_DEVICE_ID, _OTHER_DEVICE_ID),
        (_OTHER_DEVICE_ID, _DEVICE_ID, "self"),
    ],
)
def test_build_desktop_egress_route_keeps_the_hops_in_the_order_given(hops: tuple[str, ...]) -> None:
    assert build_desktop_egress_route(hops).hops == hops


@pytest.mark.parametrize(
    ("hops", "message"),
    [
        ((), "at least one place"),
        ((_DEVICE_ID, _DEVICE_ID), "same place twice"),
        (("self", "self"), "same place twice"),
        (("self", _DEVICE_ID), "must come last"),
        ((_DEVICE_ID, "self", _OTHER_DEVICE_ID), "must come last"),
        (("",), "is not 'self' or a device id"),
        (("not a device id",), "is not 'self' or a device id"),
        (("*",), "is not 'self' or a device id"),
        ((".hidden",), "is not 'self' or a device id"),
        (("device:7",), "is not 'self' or a device id"),
        ((" self", _DEVICE_ID), "is not 'self' or a device id"),
        ((_DEVICE_ID, "self "), "is not 'self' or a device id"),
        ((f" {_DEVICE_ID}", _DEVICE_ID), "is not 'self' or a device id"),
        ((f"{_DEVICE_ID}\n",), "is not 'self' or a device id"),
    ],
)
def test_build_desktop_egress_route_refuses_hops_that_do_not_form_a_route(hops: tuple[str, ...], message: str) -> None:
    with pytest.raises(DesktopEgressError, match=message):
        build_desktop_egress_route(hops)


def test_a_desktop_egress_route_cannot_be_constructed_around_the_route_rules() -> None:
    with pytest.raises(ValidationError, match="must come last"):
        DesktopEgressRoute(hops=("self", _DEVICE_ID))


@pytest.mark.parametrize(
    ("hops", "forwarders"),
    [
        (("self",), ()),
        ((_DEVICE_ID,), (_DEVICE_ID,)),
        ((_DEVICE_ID, "self"), (_DEVICE_ID,)),
        ((_OTHER_DEVICE_ID, _DEVICE_ID, "self"), (_OTHER_DEVICE_ID, _DEVICE_ID)),
    ],
)
def test_desktop_egress_forwarders_are_the_desktops_a_route_names_in_route_order(
    hops: tuple[str, ...], forwarders: tuple[str, ...]
) -> None:
    assert desktop_egress_forwarders(build_desktop_egress_route(hops)) == forwarders


@pytest.mark.parametrize(
    ("hops", "expected_mode"),
    [
        (("self",), DesktopEgressMode.OFF),
        ((_DEVICE_ID,), DesktopEgressMode.ON),
        ((_OTHER_DEVICE_ID,), DesktopEgressMode.CUSTOM),
        ((_DEVICE_ID, _OTHER_DEVICE_ID), DesktopEgressMode.CUSTOM),
        ((_OTHER_DEVICE_ID, _DEVICE_ID), DesktopEgressMode.CUSTOM),
        ((_DEVICE_ID, "self"), DesktopEgressMode.CUSTOM),
    ],
)
def test_desktop_egress_mode_for_route_is_on_only_for_the_route_through_this_computer_alone(
    hops: tuple[str, ...], expected_mode: DesktopEgressMode
) -> None:
    assert desktop_egress_mode_for_route(build_desktop_egress_route(hops), _DEVICE_ID) == expected_mode


def test_serialized_desktop_egress_rules_parse_back_to_the_same_routes() -> None:
    route_by_service_name = {
        "slack": build_desktop_egress_route((_DEVICE_ID,)),
        "github": build_desktop_egress_route((_OTHER_DEVICE_ID, _DEVICE_ID, "self")),
    }
    text = serialize_desktop_egress_rules(route_by_service_name)
    assert text == snapshot(
        """\
{
  "github": [
    "host-3f9c",
    "device-7c1f0a",
    "self"
  ],
  "slack": [
    "device-7c1f0a"
  ]
}
"""
    )
    assert parse_desktop_egress_rules(text) == DesktopEgressRules(
        route_by_service_name=route_by_service_name, legacy_enabled_service_names=()
    )


def test_desktop_egress_rules_leave_out_a_service_that_leaves_from_the_machine_alone() -> None:
    text = serialize_desktop_egress_rules(
        {"slack": SELF_ONLY_DESKTOP_EGRESS_ROUTE, "github": build_desktop_egress_route((_DEVICE_ID,))}
    )
    assert json.loads(text) == {"github": [_DEVICE_ID]}
    assert serialize_desktop_egress_rules({"slack": SELF_ONLY_DESKTOP_EGRESS_ROUTE}) == "{}\n"
    assert parse_desktop_egress_rules('{"slack": ["self"]}') == DesktopEgressRules(
        route_by_service_name={}, legacy_enabled_service_names=()
    )


def test_parse_desktop_egress_rules_lists_a_service_turned_on_with_a_bare_true_without_giving_it_a_route() -> None:
    rules = parse_desktop_egress_rules(
        f'{{"slack": true, "github": false, "linear": ["{_DEVICE_ID}"], "notion": true}}'
    )
    assert rules == DesktopEgressRules(
        route_by_service_name={"linear": build_desktop_egress_route((_DEVICE_ID,))},
        legacy_enabled_service_names=("slack", "notion"),
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "not valid JSON"),
        ("{", "not valid JSON"),
        ("[]", "not list"),
        ('"slack"', "not str"),
        ("null", "not NoneType"),
        # Python's parser accepts these; the router's JSON.parse does not.
        ('{"slack": NaN}', "no NaN"),
        ('{"slack": Infinity}', "no Infinity"),
        (f'{{"slack": "{_DEVICE_ID}"}}', "must be a list of hops"),
        ('{"slack": null}', "must be a list of hops"),
        ('{"slack": 1}', "must be a list of hops"),
        ('{"slack": {"hops": ["self"]}}', "must be a list of hops"),
        ('{"slack": [1]}', "must be a list of hops"),
        ('{"slack": [true]}', "must be a list of hops"),
        ('{"slack": []}', "rule of 'slack' cannot be used.*at least one place"),
        (f'{{"slack": ["self", "{_DEVICE_ID}"]}}', "rule of 'slack' cannot be used.*must come last"),
        (f'{{"slack": ["{_DEVICE_ID}", "{_DEVICE_ID}"]}}', "rule of 'slack' cannot be used.*same place twice"),
        ('{"slack": ["*"]}', "rule of 'slack' cannot be used.*is not 'self' or a device id"),
        ('{"slack": ["not a device id", "self"]}', "rule of 'slack' cannot be used.*is not 'self' or a device id"),
    ],
)
def test_parse_desktop_egress_rules_refuses_what_is_not_a_rules_file(text: str, message: str) -> None:
    with pytest.raises(DesktopEgressError, match=message):
        parse_desktop_egress_rules(text)


def _grants_in_file_order(*scopes_and_device_ids: tuple[str, str]) -> tuple[DesktopEgressGrant, ...]:
    return list_desktop_egress_grants(
        permissions_config_holding_grants(
            *(build_desktop_egress_grant(scope, device_id, None) for scope, device_id in scopes_and_device_ids)
        )
    )


def test_desktop_egress_route_from_grants_goes_through_the_desktop_holding_every_scope_of_the_service() -> None:
    grants = _grants_in_file_order(("slack-api", _DEVICE_ID), ("github-api", _OTHER_DEVICE_ID))
    assert desktop_egress_route_from_grants(("slack-api",), grants).hops == (_DEVICE_ID,)


def test_desktop_egress_route_from_grants_leaves_out_a_desktop_holding_only_some_scopes_of_the_service() -> None:
    grants = _grants_in_file_order(
        ("google-gmail-api", _OTHER_DEVICE_ID),
        ("google-gmail-api", _DEVICE_ID),
        ("google-calendar-api", _DEVICE_ID),
    )
    route = desktop_egress_route_from_grants(("google-gmail-api", "google-calendar-api"), grants)
    assert route.hops == (_DEVICE_ID,)


def test_desktop_egress_route_from_grants_names_several_desktops_in_file_order() -> None:
    grants = _grants_in_file_order(
        ("slack-api", _OTHER_DEVICE_ID),
        ("github-api", "third-device"),
        ("slack-api", _DEVICE_ID),
    )
    assert desktop_egress_route_from_grants(("slack-api",), grants).hops == (_OTHER_DEVICE_ID, _DEVICE_ID)


@pytest.mark.parametrize(
    "scopes_and_device_ids",
    [
        (),
        (("github-api", _DEVICE_ID),),
        (("google-gmail-api", _DEVICE_ID), ("google-calendar-api", _OTHER_DEVICE_ID)),
    ],
)
def test_desktop_egress_route_from_grants_is_the_machine_itself_when_no_desktop_holds_every_scope(
    scopes_and_device_ids: tuple[tuple[str, str], ...],
) -> None:
    grants = _grants_in_file_order(*scopes_and_device_ids)
    for service_scopes in (("slack-api",), ("google-gmail-api", "google-calendar-api")):
        assert desktop_egress_route_from_grants(service_scopes, grants) == SELF_ONLY_DESKTOP_EGRESS_ROUTE


@pytest.mark.parametrize("device_id", ["self", "not a device id"])
def test_desktop_egress_route_from_grants_leaves_out_a_grant_whose_device_id_cannot_be_a_hop(device_id: str) -> None:
    grants = _grants_in_file_order(("slack-api", device_id), ("slack-api", _DEVICE_ID))
    assert [grant.device_id for grant in grants] == [device_id, _DEVICE_ID]
    assert desktop_egress_route_from_grants(("slack-api",), grants).hops == (_DEVICE_ID,)
    assert desktop_egress_route_from_grants(("slack-api",), grants[:1]) == SELF_ONLY_DESKTOP_EGRESS_ROUTE


def test_desktop_egress_route_for_service_is_the_machine_itself_for_a_service_the_rules_do_not_name() -> None:
    rules = parse_desktop_egress_rules(f'{{"slack": ["{_DEVICE_ID}", "self"]}}')
    assert desktop_egress_route_for_service(rules.route_by_service_name, "slack").hops == (_DEVICE_ID, "self")
    assert desktop_egress_route_for_service(rules.route_by_service_name, "github") == SELF_ONLY_DESKTOP_EGRESS_ROUTE


def test_desktop_egress_route_from_grants_is_the_machine_itself_for_a_service_with_no_scopes() -> None:
    grants = list_desktop_egress_grants(
        permissions_config_holding_grants(build_desktop_egress_grant("slack-api", _DEVICE_ID, None))
    )
    assert desktop_egress_route_from_grants((), grants) == SELF_ONLY_DESKTOP_EGRESS_ROUTE
