from itertools import product

import pytest
from inline_snapshot import snapshot
from pydantic import JsonValue

from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.account_scopes import list_account_grants
from imbue.mngr_latchkey.account_scopes import resolve_account_scope
from imbue.mngr_latchkey.custom_services import build_custom_service_scope_schema
from imbue.mngr_latchkey.desktop_egress import DESKTOP_EGRESS_PERMISSIONS
from imbue.mngr_latchkey.desktop_egress import DesktopEgressError
from imbue.mngr_latchkey.desktop_egress import DesktopEgressGrant
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_grant
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_rules
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_scope_schema
from imbue.mngr_latchkey.desktop_egress import desktop_egress_scope_key
from imbue.mngr_latchkey.desktop_egress import is_service_routed
from imbue.mngr_latchkey.desktop_egress import list_desktop_egress_grants
from imbue.mngr_latchkey.desktop_egress import parse_desktop_egress_rules
from imbue.mngr_latchkey.desktop_egress import serialize_desktop_egress_rules
from imbue.mngr_latchkey.services_catalog import WILDCARD_PERMISSION_NAME
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig

_DEVICE_ID = "device-7c1f0a"


def _config_holding(*grants: tuple[str, tuple[str, ...], dict[str, JsonValue]]) -> LatchkeyPermissionsConfig:
    schemas: dict[str, JsonValue] = {}
    for _rule_key, _permissions, grant_schemas in grants:
        schemas.update(grant_schemas)
    return LatchkeyPermissionsConfig(
        rules=tuple({rule_key: list(permissions)} for rule_key, permissions, _schemas in grants),
        schemas=schemas,
    )


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
    config = _config_holding(
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
    config = LatchkeyPermissionsConfig(rules=({"both": ["any"]},), schemas={"both": both_gates})
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
    schemas: dict[str, JsonValue] = {} if schema is None else {"rule": schema}
    config = LatchkeyPermissionsConfig(rules=({"rule": ["any"]},), schemas=schemas)
    assert list_desktop_egress_grants(config) == ()


def test_per_account_readers_ignore_a_desktop_egress_grant() -> None:
    grant = build_desktop_egress_grant("slack-api", _DEVICE_ID, None)
    rule_key, _permissions, schemas = grant
    assert resolve_account_scope(schemas[rule_key]) is None
    assert list_account_grants(_config_holding(grant)) == ()


def test_build_desktop_egress_rules_turns_every_service_on_once_in_sorted_order() -> None:
    rules = build_desktop_egress_rules(["slack", "github", "slack"])
    assert list(rules.items()) == [("github", True), ("slack", True)]
    assert build_desktop_egress_rules([]) == {}


def test_serialized_desktop_egress_rules_parse_back_to_the_same_rules() -> None:
    rules = build_desktop_egress_rules(["slack", "github"])
    text = serialize_desktop_egress_rules(rules)
    assert text == snapshot(
        """\
{
  "github": true,
  "slack": true
}
"""
    )
    assert parse_desktop_egress_rules(text) == rules
    assert parse_desktop_egress_rules(serialize_desktop_egress_rules({})) == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "not valid JSON"),
        ("{", "not valid JSON"),
        ("[]", "not list"),
        ('"slack"', "not str"),
        ("null", "not NoneType"),
        # Python's parser accepts this; the router's JSON.parse does not.
        ('{"slack": NaN}', "no NaN"),
    ],
)
def test_parse_desktop_egress_rules_refuses_what_the_router_would_not_read_as_rules(text: str, message: str) -> None:
    with pytest.raises(DesktopEgressError, match=message):
        parse_desktop_egress_rules(text)


@pytest.mark.parametrize(
    ("value_json", "is_routed"),
    [
        ("true", True),
        ("false", False),
        ("null", False),
        ("0", False),
        ("0.0", False),
        ("-0.0", False),
        ('""', False),
        ("1", True),
        ("-1", True),
        ("0.5", True),
        ('"0"', True),
        ('"false"', True),
        ('"device-7c1f0a"', True),
        # Falsy in Python, truthy in the JavaScript router.
        ("[]", True),
        ("{}", True),
    ],
)
def test_is_service_routed_follows_javascript_truthiness(value_json: str, is_routed: bool) -> None:
    rules = parse_desktop_egress_rules('{"slack": ' + value_json + "}")
    assert is_service_routed(rules, "slack") is is_routed


def test_is_service_routed_is_off_for_a_service_the_rules_do_not_name() -> None:
    rules = parse_desktop_egress_rules('{"slack": true}')
    assert is_service_routed(rules, "github") is False
    assert is_service_routed({}, "slack") is False
