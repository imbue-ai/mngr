"""Unit tests for the per-workspace permission-toggle module."""

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
from pydantic import Field
from pydantic import JsonValue
from pydantic import SecretStr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.model_update import to_update
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.permission_overview import revoke_service_account_for_workspace
from imbue.minds.desktop_client.latchkey.permission_toggles import DesktopEgressRouteThroughThisComputer
from imbue.minds.desktop_client.latchkey.permission_toggles import DesktopEgressSetting
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import WorkspaceDesktop
from imbue.minds.desktop_client.latchkey.permission_toggles import WorkspacePermissionsView
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_connector_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_desktop_egress_route
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_self_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import build_desktop_egress_setting
from imbue.minds.desktop_client.latchkey.permission_toggles import build_file_sharing_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_permissions_view
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import classify_permission
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_connector_permissions
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_self_permissions
from imbue.minds.desktop_client.latchkey.permission_toggles import connect_service_with_credentials
from imbue.minds.desktop_client.latchkey.permission_toggles import describe_why_desktop_egress_is_unsupported
from imbue.minds.desktop_client.latchkey.permission_toggles import describe_why_workspace_desktop_egress_is_unsupported
from imbue.minds.desktop_client.latchkey.permission_toggles import list_workspace_desktops
from imbue.minds.desktop_client.latchkey.permission_toggles import plan_desktop_egress_grant_changes
from imbue.minds.desktop_client.latchkey.permission_toggles import put_this_computer_first_on_desktop_egress_route
from imbue.minds.desktop_client.latchkey.permission_toggles import read_workspace_desktop_egress_routes
from imbue.minds.desktop_client.latchkey.permission_toggles import resolve_desktop_egress_routes
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import FixedHostBackendResolver
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import build_permissions_test_catalog
from imbue.minds.desktop_client.latchkey.testing import seed_connector_grant
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.custom_services import build_custom_service_registration
from imbue.mngr_latchkey.custom_services import custom_service_name
from imbue.mngr_latchkey.desktop_egress import DesktopEgressMode
from imbue.mngr_latchkey.desktop_egress import DesktopEgressRoute
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_grant
from imbue.mngr_latchkey.desktop_egress import build_desktop_egress_route
from imbue.mngr_latchkey.desktop_egress import list_desktop_egress_grants
from imbue.mngr_latchkey.desktop_egress import parse_desktop_egress_rules
from imbue.mngr_latchkey.remote._mirror import store_machine_encryption_key
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import desktop_egress_rules_path_for_host
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.testing import permissions_config_holding_grants
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

_ACCOUNT = "alice@example.com"
_DEVICE_ID = "device-7f3a91"
_OTHER_DEVICE_ID = "device-c04e55"

# Slack has one scope and GitHub two.
_DESKTOP_EGRESS_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [{"scope": "slack-api", "display_name": "Slack", "permissions": [{"name": "slack-read-all"}]}],
    "github": [
        {"scope": "github-rest-api", "display_name": "GitHub", "permissions": [{"name": "github-read-all"}]},
        {"scope": "github-git", "display_name": "GitHub Git", "permissions": [{"name": "github-git-read"}]},
    ],
}


def _desktop_egress_catalog() -> ServicesCatalog:
    return ServicesCatalog.from_catalog_payload(_DESKTOP_EGRESS_CATALOG_PAYLOAD)


def _slack_info() -> ServicePermissionInfo:
    info = build_permissions_test_catalog().get_by_scope("slack-api")
    assert info is not None
    return info


@pytest.mark.parametrize(
    "permission,scope,service,expected_heading,expected_label",
    [
        ("slack-read-all", "slack-api", "slack", "Full access", "Read everything"),
        ("slack-write-all", "slack-api", "slack", "Full access", "Change everything"),
        ("slack-chat-read", "slack-api", "slack", "Chat", "Read chat"),
        ("slack-chat-write", "slack-api", "slack", "Chat", "Manage chat"),
        ("github-read-repos", "github-rest-api", "github", "Repos", "Read repos"),
        ("github-git-read", "github-git", "github", "Full access", "Read everything"),
        ("google-gmail-send-messages", "google-gmail-api", "google-gmail", "Messages", "Send messages"),
        ("aws-s3", "aws", "aws", "S3", "S3"),
        ("slack-search", "slack-api", "slack", "Search", "Search"),
        ("everything", "claude-ai", "claude-ai", "Full access", "Everything"),
        ("any", "slack-api", "slack", "Extras", "Everything (unrestricted)"),
    ],
)
def test_classify_permission_covers_the_catalog_naming_conventions(
    permission: str,
    scope: str,
    service: str,
    expected_heading: str,
    expected_label: str,
) -> None:
    """The heuristic handles verb-last, verb-first, whole-scope, bare, and wildcard names."""
    _, heading, label = classify_permission(permission, scope, service)
    assert (heading, label) == (expected_heading, expected_label)


def test_compute_connector_permissions_returns_the_full_set_after_a_flip() -> None:
    info = _slack_info()
    enabled = compute_connector_permissions(info, ("slack-read-all",), "slack-chat-write", True)
    assert enabled == ("slack-read-all", "slack-chat-write")
    disabled = compute_connector_permissions(info, enabled, "slack-read-all", False)
    assert disabled == ("slack-chat-write",)


def test_compute_connector_permissions_keeps_catalog_order_and_unknown_names() -> None:
    """Hand-edited grants outside the catalog survive a flip verbatim, appended after catalog names."""
    info = _slack_info()
    current = ("hand-edited-extra", "slack-chat-read")
    updated = compute_connector_permissions(info, current, "slack-read-all", True)
    assert updated == ("slack-read-all", "slack-chat-read", "hand-edited-extra")


def test_compute_connector_permissions_can_empty_the_set_and_grant_the_wildcard() -> None:
    info = _slack_info()
    assert compute_connector_permissions(info, ("slack-read-all",), "slack-read-all", False) == ()
    assert compute_connector_permissions(info, (), "any", True) == ("any",)


def test_compute_connector_permissions_rejects_a_permission_outside_the_catalog() -> None:
    with pytest.raises(PermissionToggleError):
        compute_connector_permissions(_slack_info(), (), "slack-users-read", True)


_SHARED_PATH_PERMISSION = f"minds-file-server-read-{_DEVICE_ID}:/Users/me/notes"
_VERB_PERMISSION = WORKSPACE_VERBS[0].permission
_BASELINE_PERMISSION = "minds-api-proxy-call-agent-123"


def _self_config(granted: tuple[str, ...], schemas: dict[str, JsonValue] | None = None) -> LatchkeyPermissionsConfig:
    return LatchkeyPermissionsConfig(rules=({SELF_SCOPE: list(granted)},), schemas=schemas or {})


def test_compute_self_permissions_disable_preserves_unrelated_names() -> None:
    config = _self_config((_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION, _VERB_PERMISSION))
    updated = compute_self_permissions(config, _SHARED_PATH_PERMISSION, False)
    assert updated == (_BASELINE_PERMISSION, _VERB_PERMISSION)


def test_compute_self_permissions_enable_requires_the_schema_definition() -> None:
    config = _self_config((_BASELINE_PERMISSION,), schemas={_SHARED_PATH_PERMISSION: {"type": "object"}})
    updated = compute_self_permissions(config, _SHARED_PATH_PERMISSION, True)
    assert updated == (_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION)
    with pytest.raises(PermissionToggleError):
        compute_self_permissions(_self_config((_BASELINE_PERMISSION,)), _SHARED_PATH_PERMISSION, True)


def test_compute_self_permissions_is_none_for_a_no_op_flip() -> None:
    config = _self_config((_SHARED_PATH_PERMISSION,))
    assert compute_self_permissions(config, _SHARED_PATH_PERMISSION, True) is None
    assert compute_self_permissions(_self_config(()), _SHARED_PATH_PERMISSION, False) is None


def test_compute_self_permissions_rejects_non_toggleable_names() -> None:
    """Baseline / accounts names on the shared rule must not be reachable from the toggle routes."""
    with pytest.raises(PermissionToggleError):
        compute_self_permissions(_self_config((_BASELINE_PERMISSION,)), _BASELINE_PERMISSION, False)


def test_build_file_sharing_toggles_includes_revoked_but_restorable_paths() -> None:
    """A path whose schema is still in the file renders as an off toggle that can be re-enabled."""
    write_permission = f"minds-file-server-write-{_DEVICE_ID}:/Users/me/notes"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
        schemas={_SHARED_PATH_PERMISSION: {"type": "object"}, write_permission: {"type": "object"}},
    )
    toggles = build_file_sharing_toggles(config, _DEVICE_ID)
    assert [(toggle.permission, toggle.is_granted, toggle.can_enable) for toggle in toggles] == [
        (_SHARED_PATH_PERMISSION, True, True),
        (write_permission, False, True),
    ]
    assert toggles[0].label == "/Users/me/notes"
    assert toggles[0].detail == "read"
    assert toggles[1].detail == "read and write"


def test_build_file_sharing_toggles_lists_only_this_desktops_paths() -> None:
    """Another desktop's grant on the same machine is not this pane's row."""
    other_desktops_permission = f"minds-file-server-read-{_OTHER_DEVICE_ID}:/Users/other/notes"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_SHARED_PATH_PERMISSION, other_desktops_permission]},),
        schemas={name: {"type": "object"} for name in (_SHARED_PATH_PERMISSION, other_desktops_permission)},
    )

    toggles = build_file_sharing_toggles(config, _DEVICE_ID)

    assert [toggle.permission for toggle in toggles] == [_SHARED_PATH_PERMISSION]
    assert build_file_sharing_toggles(config, "") == ()


# CLEANUP: drop the tests of grants from before devices below with the code they cover, once no
# policy carries a device-less file-sharing grant.
_LEGACY_SHARED_PATH_PERMISSION = "minds-file-server-read-/Users/me/notes"


def test_build_file_sharing_toggles_lists_a_granted_path_from_before_devices_as_revoke_only() -> None:
    """Every desktop serves such a path at the device-less URL, so every desktop lists it, whoever granted it."""
    revoked_legacy_permission = "minds-file-server-write-/Users/me/notes"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _LEGACY_SHARED_PATH_PERMISSION]},),
        schemas={name: {"type": "object"} for name in (_LEGACY_SHARED_PATH_PERMISSION, revoked_legacy_permission)},
    )

    toggles = build_file_sharing_toggles(config, _DEVICE_ID)

    assert [
        (toggle.permission, toggle.label, toggle.detail, toggle.is_granted, toggle.can_enable) for toggle in toggles
    ] == [
        (_LEGACY_SHARED_PATH_PERMISSION, "/Users/me/notes", "read", True, False),
    ]


def test_compute_self_permissions_revokes_the_grant_from_before_devices_with_its_twin() -> None:
    """Otherwise the path stays reachable at the device-less URL after the user stopped sharing it."""
    other_legacy_permission = "minds-file-server-write-/Users/me/notes"
    config = _self_config(
        (_BASELINE_PERMISSION, _LEGACY_SHARED_PATH_PERMISSION, _SHARED_PATH_PERMISSION, other_legacy_permission)
    )

    assert compute_self_permissions(config, _SHARED_PATH_PERMISSION, False) == (
        _BASELINE_PERMISSION,
        other_legacy_permission,
    )


def test_compute_self_permissions_revokes_a_grant_from_before_devices_that_has_no_twin() -> None:
    """On a desktop other than the one that migrated the policy, the old grant is all there is for the path."""
    config = _self_config((_BASELINE_PERMISSION, _LEGACY_SHARED_PATH_PERMISSION))

    assert compute_self_permissions(config, _SHARED_PATH_PERMISSION, False) == (_BASELINE_PERMISSION,)


def test_compute_self_permissions_refuses_a_name_from_before_devices() -> None:
    """Nothing grants such a name any more, including turning back on one whose schema is still in the file."""
    config = _self_config((_BASELINE_PERMISSION,), schemas={_LEGACY_SHARED_PATH_PERMISSION: {"type": "object"}})

    with pytest.raises(PermissionToggleError, match="not toggleable"):
        compute_self_permissions(config, _LEGACY_SHARED_PATH_PERMISSION, True)


def test_compute_self_permissions_enabling_a_shared_path_leaves_the_name_from_before_devices_off() -> None:
    schemas: dict[str, JsonValue] = {
        _SHARED_PATH_PERMISSION: {"type": "object"},
        _LEGACY_SHARED_PATH_PERMISSION: {"type": "object"},
    }

    updated = compute_self_permissions(_self_config((_BASELINE_PERMISSION,), schemas), _SHARED_PATH_PERMISSION, True)

    assert updated == (_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION)


def test_toggles_report_a_grant_whose_schema_is_gone_as_not_re_enableable() -> None:
    """``can_enable`` answers "could this be turned back on", which needs the schema.

    Turning such a row off is a one-way door: detent fails the whole check on an
    unresolvable reference, so :func:`compute_self_permissions` refuses to put
    the name back and only the agent re-requesting brings it back.
    """
    verb = next(verb for verb in WORKSPACE_VERBS if not verb.is_targeted)
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION, verb.permission]},),
        schemas={},
    )

    file_sharing = build_file_sharing_toggles(config, _DEVICE_ID)
    workspace = build_workspace_toggles(StaticBackendResolver(url_by_agent_and_service={}), config)

    assert [(toggle.permission, toggle.is_granted, toggle.can_enable) for toggle in file_sharing] == [
        (_SHARED_PATH_PERMISSION, True, False),
    ]
    assert [(toggle.permission, toggle.is_granted, toggle.can_enable) for toggle in workspace] == [
        (verb.permission, True, False),
    ]
    with pytest.raises(PermissionToggleError, match="its definition is gone"):
        compute_self_permissions(_self_config((_BASELINE_PERMISSION,)), _SHARED_PATH_PERMISSION, True)


def test_build_workspace_toggles_labels_verbs_and_targets() -> None:
    target_agent = str(AgentId())
    targeted_verb = next(verb for verb in WORKSPACE_VERBS if verb.is_targeted)
    untargeted_verb = next(verb for verb in WORKSPACE_VERBS if not verb.is_targeted)
    targeted_name = f"{targeted_verb.permission}-{target_agent}"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [untargeted_verb.permission, targeted_name]},),
        schemas={},
    )
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    toggles = build_workspace_toggles(resolver, config)
    by_permission = {toggle.permission: toggle for toggle in toggles}
    assert by_permission[untargeted_verb.permission].detail == "All machines"
    assert by_permission[untargeted_verb.permission].label == untargeted_verb.display_name
    # The resolver knows nothing, so the target falls back to its raw agent id.
    assert by_permission[targeted_name].detail == target_agent
    assert by_permission[targeted_name].description == targeted_verb.description


def test_build_workspace_permissions_view_marks_granted_toggles(tmp_path: Path) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    seed_connector_grant(latchkey.plugin_data_dir, host, "slack-api", _ACCOUNT, ("slack-chat-read",))
    resolver = FixedHostBackendResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        machine_latchkey=latchkey,
        workspace_agent_id=str(agent_id),
        device_id=_DEVICE_ID,
    )

    assert view.host_id == str(host)
    # Slack is connected, so it renders as a connection; GitHub has no account
    # and no grants, so it is offered under Add connection.
    assert [connection.service_name for connection in view.connections] == ["slack"]
    assert [service.service_name for service in view.available_connections] == ["aws", "github"]
    connection = view.connections[0]
    assert connection.account == _ACCOUNT
    assert connection.is_connected
    assert connection.granted_count == 1
    toggle_states = {
        toggle.permission: toggle.is_granted for group in connection.scopes[0].groups for toggle in group.toggles
    }
    assert toggle_states["slack-chat-read"] is True
    assert toggle_states["slack-read-all"] is False
    assert toggle_states["any"] is False


def test_build_workspace_permissions_view_lists_granted_but_disconnected_accounts(tmp_path: Path) -> None:
    """Grants for an account latchkey no longer stores still render (so they can be revoked)."""
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    seed_connector_grant(latchkey.plugin_data_dir, host, "slack-api", _ACCOUNT, ("slack-read-all",))
    resolver = FixedHostBackendResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        machine_latchkey=latchkey,
        workspace_agent_id=str(agent_id),
        device_id=_DEVICE_ID,
    )

    assert [connection.service_name for connection in view.connections] == ["slack"]
    assert not view.connections[0].is_connected
    # A disconnected-but-granted service must not also appear as addable.
    assert [service.service_name for service in view.available_connections] == ["aws", "github"]


def test_build_workspace_toggles_includes_revoked_but_restorable_verbs() -> None:
    """A revoked verb whose per-target schema survives still renders as an off toggle.

    Leaving the schema behind on revoke is exactly what makes the row
    re-enableable, so it has to reach the pane -- the same guarantee the
    file-sharing rows have.
    """
    targeted_verb = next(verb for verb in WORKSPACE_VERBS if verb.is_targeted)
    granted_target, revoked_target = str(AgentId()), str(AgentId())
    granted_name = f"{targeted_verb.permission}-{granted_target}"
    revoked_name = f"{targeted_verb.permission}-{revoked_target}"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [granted_name]},),
        schemas={granted_name: {"type": "object"}, revoked_name: {"type": "object"}},
    )

    toggles = build_workspace_toggles(StaticBackendResolver(url_by_agent_and_service={}), config)

    by_permission = {toggle.permission: toggle for toggle in toggles}
    assert (by_permission[granted_name].is_granted, by_permission[granted_name].can_enable) == (True, True)
    assert (by_permission[revoked_name].is_granted, by_permission[revoked_name].can_enable) == (False, True)
    assert by_permission[revoked_name].detail == revoked_target


def test_build_workspace_permissions_view_leads_a_service_with_its_connected_accounts(tmp_path: Path) -> None:
    """Within a service the nav reads connected first, then orphaned grants, default last.

    The same order the settings page uses for the same data -- and not
    alphabetical by label, which would lead with "Default account" and put an
    account that is no longer connected above one that is.
    """
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": ["zoe@x"]},
    )
    rules: list[dict[str, list[str]]] = []
    schemas: dict[str, JsonValue] = {}
    for account in ("alice@x", DEFAULT_ACCOUNT):
        rule_key, granted, account_schemas = build_account_grant("slack-api", account, ("slack-chat-read",))
        rules.append({rule_key: list(granted)})
        schemas.update(account_schemas)
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host),
        LatchkeyPermissionsConfig(rules=tuple(rules), schemas=schemas),
    )

    view = _build_view(latchkey, agent_id, host)

    assert [connection.account for connection in view.connections] == ["zoe@x", "alice@x", DEFAULT_ACCOUNT]


def _build_view(
    latchkey: FakeAccountsLatchkey,
    agent_id: AgentId,
    host: HostId,
    machine_latchkey: FakeAccountsLatchkey | None = None,
    device_id: str = _DEVICE_ID,
    services_catalog: ServicesCatalog | None = None,
) -> WorkspacePermissionsView:
    return build_workspace_permissions_view(
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)
        ),
        gateway_client=build_fake_gateway_client(),
        services_catalog=services_catalog if services_catalog is not None else build_permissions_test_catalog(),
        latchkey=latchkey,
        machine_latchkey=machine_latchkey if machine_latchkey is not None else latchkey,
        workspace_agent_id=str(agent_id),
        device_id=device_id,
    )


def test_build_workspace_permissions_view_reports_whose_store_the_machine_reads(tmp_path: Path) -> None:
    """What the pane's Disconnect copy turns on: who else loses the sign-in."""
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path / "desktop", latchkey_binary="/nonexistent")
    machine_latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path / "machine", latchkey_binary="/nonexistent")

    assert _build_view(latchkey, agent_id, host).is_credential_store_shared is True
    assert _build_view(latchkey, agent_id, host, machine_latchkey=machine_latchkey).is_credential_store_shared is False


def test_build_workspace_permissions_view_carries_how_each_service_is_connected(tmp_path: Path) -> None:
    """A browser-less service travels with the inputs its own command asks for."""
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    view = _build_view(latchkey, agent_id, host)

    sign_in_by_service = {entry.service_name: entry.sign_in for entry in view.available_connections}
    assert sign_in_by_service["github"].is_browser_supported is True
    assert sign_in_by_service["github"].credential_parameters == ()
    aws_sign_in = sign_in_by_service["aws"]
    assert aws_sign_in.is_browser_supported is False
    assert [(parameter.name, parameter.label) for parameter in aws_sign_in.credential_parameters] == [
        ("access-key-id", "Access key id"),
        ("secret-access-key", "Secret access key"),
    ]
    # AWS has no account yet, so the next one is latchkey's unnamed default.
    assert aws_sign_in.is_account_name_required is False


def test_build_workspace_permissions_view_asks_a_further_account_for_a_name(tmp_path: Path) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": ["work"]},
    )

    view = _build_view(latchkey, agent_id, host)

    aws = next(connection for connection in view.connections if connection.service_name == "aws")
    assert aws.sign_in.is_browser_supported is False
    assert aws.sign_in.is_account_name_required is True


def test_build_workspace_permissions_view_offers_no_form_for_an_unusable_command(tmp_path: Path) -> None:
    """A command with no ``<placeholder>`` leaves nothing to ask for, so no form is offered."""
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        credential_example_by_service={"aws": "latchkey auth set-nocurl aws"},
    )

    view = _build_view(latchkey, agent_id, host)

    aws = next(entry for entry in view.available_connections if entry.service_name == "aws")
    assert aws.sign_in.is_browser_supported is False
    assert aws.sign_in.credential_parameters == ()


def test_build_workspace_permissions_view_rejects_unknown_workspaces(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    with pytest.raises(PermissionToggleError):
        build_workspace_permissions_view(
            backend_resolver=resolver,
            gateway_client=build_fake_gateway_client(),
            services_catalog=build_permissions_test_catalog(),
            latchkey=latchkey,
            machine_latchkey=latchkey,
            workspace_agent_id=str(AgentId()),
            device_id=_DEVICE_ID,
        )


def _egress_config(scopes_and_device_ids: Sequence[tuple[str, str]]) -> LatchkeyPermissionsConfig:
    """A permissions file holding one desktop egress grant per ``(scope, device_id)``, in order."""
    return permissions_config_holding_grants(
        *(build_desktop_egress_grant(scope, device_id, None) for scope, device_id in scopes_and_device_ids)
    )


def _egress_rule_keys(scopes_and_device_ids: Sequence[tuple[str, str]]) -> list[str]:
    return sorted(build_desktop_egress_grant(scope, device_id, None)[0] for scope, device_id in scopes_and_device_ids)


@pytest.mark.parametrize(
    "device_id,is_machine_of_its_own,expected_fragment",
    [
        ("", True, "device id"),
        (_DEVICE_ID, False, "machine of its own"),
    ],
)
def test_describe_why_desktop_egress_is_unsupported_names_the_condition_that_is_not_met(
    device_id: str,
    is_machine_of_its_own: bool,
    expected_fragment: str,
) -> None:
    reason = describe_why_desktop_egress_is_unsupported(device_id, is_machine_of_its_own)
    assert reason is not None
    assert expected_fragment in reason


def test_describe_why_desktop_egress_is_unsupported_is_none_when_every_condition_is_met() -> None:
    assert describe_why_desktop_egress_is_unsupported(_DEVICE_ID, True) is None


@pytest.mark.parametrize(
    "device_id,is_machine_of_its_own",
    [
        ("", True),
        (_DEVICE_ID, False),
    ],
)
def test_build_desktop_egress_setting_shows_an_unsupported_setting_as_off_whatever_the_route(
    device_id: str,
    is_machine_of_its_own: bool,
) -> None:
    """A route left behind in the rules copy does not make an unsupported setting read as on."""
    setting = build_desktop_egress_setting(device_id, is_machine_of_its_own, build_desktop_egress_route((_DEVICE_ID,)))

    assert setting == DesktopEgressSetting(is_supported=False, mode=DesktopEgressMode.OFF, route=("self",))


@pytest.mark.parametrize(
    "hops,expected_mode",
    [
        (("self",), DesktopEgressMode.OFF),
        ((_DEVICE_ID,), DesktopEgressMode.ON),
        ((_OTHER_DEVICE_ID,), DesktopEgressMode.CUSTOM),
        ((_DEVICE_ID, "self"), DesktopEgressMode.CUSTOM),
        ((_OTHER_DEVICE_ID, _DEVICE_ID), DesktopEgressMode.CUSTOM),
    ],
)
def test_build_desktop_egress_setting_reports_the_mode_and_hops_of_a_supported_route(
    hops: tuple[str, ...],
    expected_mode: DesktopEgressMode,
) -> None:
    setting = build_desktop_egress_setting(_DEVICE_ID, True, build_desktop_egress_route(hops))

    assert setting == DesktopEgressSetting(is_supported=True, mode=expected_mode, route=hops)


def test_plan_desktop_egress_grant_changes_writes_the_whole_grant_of_every_scope_of_the_service() -> None:
    catalog = _desktop_egress_catalog()

    changes = plan_desktop_egress_grant_changes(
        LatchkeyPermissionsConfig(), {"github": build_desktop_egress_route((_DEVICE_ID,))}, catalog.as_mapping()
    )

    assert changes.grants_to_write == tuple(
        build_desktop_egress_grant(info.scope, _DEVICE_ID, info.scope_schema) for info in catalog.get("github")
    )
    assert len(changes.grants_to_write) == 2
    assert changes.rule_keys_to_delete == ()


_GITHUB_SCOPES = ("github-rest-api", "github-git")


@pytest.mark.parametrize(
    "existing,hops_by_service_name,expected_written,expected_deleted",
    [
        pytest.param(
            (),
            {"github": (_DEVICE_ID, _OTHER_DEVICE_ID, "self")},
            tuple((scope, device_id) for scope in _GITHUB_SCOPES for device_id in (_DEVICE_ID, _OTHER_DEVICE_ID)),
            (),
            id="two_desktops_need_a_grant_per_desktop_per_scope",
        ),
        pytest.param(
            (("github-rest-api", _DEVICE_ID),),
            {"github": (_DEVICE_ID,)},
            (("github-git", _DEVICE_ID),),
            (),
            id="a_grant_already_present_is_not_written_again",
        ),
        pytest.param(
            (("slack-api", _DEVICE_ID), ("github-git", _DEVICE_ID), ("github-git", _OTHER_DEVICE_ID)),
            {"github": (_OTHER_DEVICE_ID,)},
            (("github-rest-api", _OTHER_DEVICE_ID),),
            (("slack-api", _DEVICE_ID), ("github-git", _DEVICE_ID)),
            id="grants_no_route_needs_are_deleted",
        ),
        pytest.param(
            (("linear-api", _DEVICE_ID), ("linear-api", _OTHER_DEVICE_ID), ("slack-api", _DEVICE_ID)),
            {},
            (),
            (("slack-api", _DEVICE_ID),),
            id="a_grant_on_a_scope_outside_the_catalog_is_left_alone",
        ),
        pytest.param(
            (("slack-api", _DEVICE_ID),),
            {"slack": ("self",), "github": ("self",)},
            (),
            (("slack-api", _DEVICE_ID),),
            id="a_self_only_route_needs_none",
        ),
    ],
)
def test_plan_desktop_egress_grant_changes_brings_the_grants_in_line_with_the_routes(
    existing: tuple[tuple[str, str], ...],
    hops_by_service_name: dict[str, tuple[str, ...]],
    expected_written: tuple[tuple[str, str], ...],
    expected_deleted: tuple[tuple[str, str], ...],
) -> None:
    config = _egress_config(existing)
    # Every seeded grant has to read as a desktop egress grant, or a "left alone" case would pass vacuously.
    assert len(list_desktop_egress_grants(config)) == len(existing)

    changes = plan_desktop_egress_grant_changes(
        config,
        {name: build_desktop_egress_route(hops) for name, hops in hops_by_service_name.items()},
        _desktop_egress_catalog().as_mapping(),
    )

    assert sorted(rule_key for rule_key, _, _ in changes.grants_to_write) == _egress_rule_keys(expected_written)
    assert sorted(changes.rule_keys_to_delete) == _egress_rule_keys(expected_deleted)


@pytest.mark.parametrize(
    "rules,granted,expected_hops_by_service_name",
    [
        pytest.param(
            {"github": True},
            tuple((scope, _DEVICE_ID) for scope in _GITHUB_SCOPES),
            {"github": (_DEVICE_ID,)},
            id="a_legacy_service_goes_through_the_desktop_holding_its_grants",
        ),
        pytest.param(
            {"github": True},
            (
                ("github-rest-api", _OTHER_DEVICE_ID),
                *((scope, _DEVICE_ID) for scope in _GITHUB_SCOPES),
                ("github-git", _OTHER_DEVICE_ID),
            ),
            {"github": (_OTHER_DEVICE_ID, _DEVICE_ID)},
            id="every_desktop_holding_them_is_a_hop_in_file_order",
        ),
        pytest.param(
            {"github": True},
            (*((scope, _DEVICE_ID) for scope in _GITHUB_SCOPES), ("github-rest-api", _OTHER_DEVICE_ID)),
            {"github": (_DEVICE_ID,)},
            id="a_desktop_missing_one_scope_is_not_a_hop",
        ),
        pytest.param(
            {"github": True},
            (("github-rest-api", _DEVICE_ID), ("github-git", _OTHER_DEVICE_ID)),
            {},
            id="a_legacy_service_no_desktop_holds_every_scope_of_is_dropped",
        ),
        pytest.param(
            {"linear": True},
            (("linear-api", _DEVICE_ID),),
            {},
            id="a_legacy_service_the_catalog_does_not_know_is_dropped",
        ),
        pytest.param(
            {"slack": [_OTHER_DEVICE_ID, "self"], "github": True},
            tuple((scope, _DEVICE_ID) for scope in _GITHUB_SCOPES),
            {"slack": (_OTHER_DEVICE_ID, "self"), "github": (_DEVICE_ID,)},
            id="a_route_the_file_gives_is_kept_whatever_the_grants",
        ),
    ],
)
def test_resolve_desktop_egress_routes_reads_a_legacy_services_route_off_the_grants(
    rules: dict[str, JsonValue],
    granted: tuple[tuple[str, str], ...],
    expected_hops_by_service_name: dict[str, tuple[str, ...]],
) -> None:
    grants = list_desktop_egress_grants(_egress_config(granted))
    assert len(grants) == len(granted)

    route_by_service_name = resolve_desktop_egress_routes(
        parse_desktop_egress_rules(json.dumps(rules)), grants, _desktop_egress_catalog().as_mapping()
    )

    assert route_by_service_name == {
        name: DesktopEgressRoute(hops=hops) for name, hops in expected_hops_by_service_name.items()
    }


def _desktop_egress_latchkey(
    tmp_path: Path,
    accounts_by_service: dict[str, list[str]] | None = None,
) -> FakeAccountsLatchkey:
    return FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service=accounts_by_service if accounts_by_service is not None else {"slack": [_ACCOUNT]},
    )


def _give_host_a_machine_of_its_own(latchkey: FakeAccountsLatchkey, host: HostId) -> None:
    store_machine_encryption_key(latchkey.plugin_data_dir, host, SecretStr("machine-key-5820"))


def _grant_desktop_egress(latchkey: FakeAccountsLatchkey, host: HostId, scope: str, device_id: str) -> None:
    rule_key, permissions, schemas = build_desktop_egress_grant(scope, device_id, None)
    build_fake_gateway_client().set_permission_rule(
        permissions_path_for_host(latchkey.plugin_data_dir, host), rule_key, permissions, schemas
    )


def _write_desktop_egress_rules_copy(latchkey: FakeAccountsLatchkey, host: HostId, rules_json: str) -> None:
    rules_path = desktop_egress_rules_path_for_host(latchkey.plugin_data_dir, host)
    rules_path.parent.mkdir(parents=True, exist_ok=True)
    rules_path.write_text(rules_json)


_THIRD_DEVICE_ID = "device-19bd02"


def _desktop_egress_settings(view: WorkspacePermissionsView) -> list[tuple[str, DesktopEgressSetting]]:
    """``(service_name, setting)`` of every connection panel, in nav order."""
    return [(connection.service_name, connection.desktop_egress) for connection in view.connections]


_UNSUPPORTED_SETTING = DesktopEgressSetting(is_supported=False, mode=DesktopEgressMode.OFF, route=("self",))
_OFF_SETTING = DesktopEgressSetting(is_supported=True, mode=DesktopEgressMode.OFF, route=("self",))
_ON_SETTING = DesktopEgressSetting(is_supported=True, mode=DesktopEgressMode.ON, route=(_DEVICE_ID,))


@pytest.mark.parametrize(
    "device_id,is_machine_of_its_own",
    [
        ("", True),
        (_DEVICE_ID, False),
    ],
)
def test_view_offers_no_desktop_egress_and_no_desktops_where_it_is_unsupported(
    tmp_path: Path,
    device_id: str,
    is_machine_of_its_own: bool,
) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    if is_machine_of_its_own:
        _give_host_a_machine_of_its_own(latchkey, host)
    _write_desktop_egress_rules_copy(latchkey, host, json.dumps({"slack": [_OTHER_DEVICE_ID]}))

    view = _build_view(latchkey, agent_id, host, device_id=device_id)

    assert _desktop_egress_settings(view) == [("slack", _UNSUPPORTED_SETTING)]
    assert view.desktops == ()


def test_view_carries_each_services_setting_from_the_rules_copy_on_every_account(tmp_path: Path) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _desktop_egress_latchkey(
        tmp_path, accounts_by_service={"slack": [_ACCOUNT, "bob@example.com"], "github": [_ACCOUNT]}
    )
    _give_host_a_machine_of_its_own(latchkey, host)

    def _settings() -> list[tuple[str, DesktopEgressSetting]]:
        return _desktop_egress_settings(
            _build_view(latchkey, agent_id, host, services_catalog=_desktop_egress_catalog())
        )

    assert _settings() == [("github", _OFF_SETTING), ("slack", _OFF_SETTING), ("slack", _OFF_SETTING)]

    _write_desktop_egress_rules_copy(
        latchkey, host, json.dumps({"github": [_DEVICE_ID], "slack": [_OTHER_DEVICE_ID, "self"]})
    )
    slack_setting = DesktopEgressSetting(
        is_supported=True, mode=DesktopEgressMode.CUSTOM, route=(_OTHER_DEVICE_ID, "self")
    )
    assert _settings() == [("github", _ON_SETTING), ("slack", slack_setting), ("slack", slack_setting)]


@pytest.mark.parametrize(
    "rules_json,expected_setting",
    [
        pytest.param("[not rules", _OFF_SETTING, id="an_unparseable_copy_routes_nothing"),
        pytest.param(json.dumps({"slack": [_DEVICE_ID]}), _ON_SETTING, id="a_route_through_this_computer_alone_is_on"),
        pytest.param(json.dumps({"github": [_DEVICE_ID]}), _OFF_SETTING, id="another_services_route_is_not_slacks"),
    ],
)
def test_view_reads_the_slack_setting_off_the_rules_copy(
    tmp_path: Path,
    rules_json: str,
    expected_setting: DesktopEgressSetting,
) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    _give_host_a_machine_of_its_own(latchkey, host)
    _write_desktop_egress_rules_copy(latchkey, host, rules_json)

    assert _desktop_egress_settings(_build_view(latchkey, agent_id, host)) == [("slack", expected_setting)]


@pytest.mark.parametrize(
    "granted_device_ids,expected_setting",
    [
        pytest.param((_DEVICE_ID,), _ON_SETTING, id="this_computer_holding_the_grants_reads_as_on"),
        pytest.param(
            (_OTHER_DEVICE_ID,),
            DesktopEgressSetting(is_supported=True, mode=DesktopEgressMode.CUSTOM, route=(_OTHER_DEVICE_ID,)),
            id="only_another_desktop_holding_them_reads_as_custom",
        ),
        pytest.param((), _OFF_SETTING, id="no_desktop_holding_them_reads_as_off"),
    ],
)
def test_view_reads_the_setting_of_a_legacy_true_service_off_the_grants(
    tmp_path: Path,
    granted_device_ids: tuple[str, ...],
    expected_setting: DesktopEgressSetting,
) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    _give_host_a_machine_of_its_own(latchkey, host)
    _write_desktop_egress_rules_copy(latchkey, host, json.dumps({"slack": True}))
    for granted_device_id in granted_device_ids:
        _grant_desktop_egress(latchkey, host, "slack-api", granted_device_id)

    view = _build_view(latchkey, agent_id, host, services_catalog=_desktop_egress_catalog())

    assert _desktop_egress_settings(view) == [("slack", expected_setting)]


def test_view_lists_this_computer_and_the_desktops_its_routes_name_when_desktop_egress_is_supported(
    tmp_path: Path,
) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    _give_host_a_machine_of_its_own(latchkey, host)
    this_computer = WorkspaceDesktop(device_id=_DEVICE_ID, is_this_computer=True)

    assert _build_view(latchkey, agent_id, host).desktops == (this_computer,)

    _write_desktop_egress_rules_copy(latchkey, host, json.dumps({"slack": [_OTHER_DEVICE_ID, _DEVICE_ID, "self"]}))

    assert _build_view(latchkey, agent_id, host).desktops == (
        this_computer,
        WorkspaceDesktop(device_id=_OTHER_DEVICE_ID, is_this_computer=False),
    )


def test_list_workspace_desktops_leads_with_this_computer_then_names_each_other_desktop_once() -> None:
    route_by_service_name = {
        "slack": build_desktop_egress_route((_OTHER_DEVICE_ID, _DEVICE_ID, "self")),
        "github": build_desktop_egress_route((_THIRD_DEVICE_ID, _OTHER_DEVICE_ID)),
    }

    # The others follow the services by name, then each route's own order.
    assert list_workspace_desktops(_DEVICE_ID, route_by_service_name) == (
        WorkspaceDesktop(device_id=_DEVICE_ID, is_this_computer=True),
        WorkspaceDesktop(device_id=_THIRD_DEVICE_ID, is_this_computer=False),
        WorkspaceDesktop(device_id=_OTHER_DEVICE_ID, is_this_computer=False),
    )


@pytest.mark.parametrize("hops_by_service_name", [{}, {"slack": ("self",)}, {"slack": (_DEVICE_ID, "self")}])
def test_list_workspace_desktops_offers_this_computer_alone_when_no_route_names_another(
    hops_by_service_name: dict[str, tuple[str, ...]],
) -> None:
    route_by_service_name = {name: build_desktop_egress_route(hops) for name, hops in hops_by_service_name.items()}

    assert list_workspace_desktops(_DEVICE_ID, route_by_service_name) == (
        WorkspaceDesktop(device_id=_DEVICE_ID, is_this_computer=True),
    )


def test_list_workspace_desktops_names_no_computer_as_this_one_without_a_device_id() -> None:
    route_by_service_name = {"slack": build_desktop_egress_route((_OTHER_DEVICE_ID, "self"))}

    assert list_workspace_desktops("", route_by_service_name) == (
        WorkspaceDesktop(device_id=_OTHER_DEVICE_ID, is_this_computer=False),
    )
    assert list_workspace_desktops("", {}) == ()


@pytest.mark.parametrize(
    "rules_json,expected_hops_by_service_name",
    [
        (None, {}),
        (
            json.dumps({"slack": [_OTHER_DEVICE_ID, _DEVICE_ID, "self"], "github": ["self"]}),
            {"slack": (_OTHER_DEVICE_ID, _DEVICE_ID, "self")},
        ),
        (json.dumps({"slack": True, "github": True}), {"slack": (_DEVICE_ID,)}),
        ("[not rules", {}),
    ],
)
def test_read_workspace_desktop_egress_routes_reads_every_route_off_the_rules_copy(
    tmp_path: Path,
    rules_json: str | None,
    expected_hops_by_service_name: dict[str, tuple[str, ...]],
) -> None:
    host = HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    if rules_json is not None:
        _write_desktop_egress_rules_copy(latchkey, host, rules_json)

    route_by_service_name = read_workspace_desktop_egress_routes(
        latchkey.plugin_data_dir,
        host,
        _egress_config((("slack-api", _DEVICE_ID),)),
        _desktop_egress_catalog().as_mapping(),
    )

    assert route_by_service_name == {
        name: DesktopEgressRoute(hops=hops) for name, hops in expected_hops_by_service_name.items()
    }


def test_describe_why_workspace_desktop_egress_is_unsupported_checks_the_hosts_machine_store(tmp_path: Path) -> None:
    host = HostId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    data_dir = latchkey.plugin_data_dir

    without_machine = describe_why_workspace_desktop_egress_is_unsupported(data_dir, host, _DEVICE_ID)
    assert without_machine is not None
    assert "machine of its own" in without_machine

    _give_host_a_machine_of_its_own(latchkey, host)
    assert describe_why_workspace_desktop_egress_is_unsupported(data_dir, host, _DEVICE_ID) is None
    without_device_id = describe_why_workspace_desktop_egress_is_unsupported(data_dir, host, "")
    assert without_device_id is not None
    assert "device id" in without_device_id


class _ToggleHarness(FrozenModel):
    """The typed dependency bundle the apply_* functions take, plus the host file path."""

    backend_resolver: FixedHostBackendResolver = Field(description="Resolver mapping the test agent to its host.")
    gateway_client: FakeLatchkeyGatewayClient = Field(description="Fake gateway writing a real on-disk file.")
    services_catalog: ServicesCatalog = Field(description="Catalog built from the test payload.")
    latchkey: FakeAccountsLatchkey = Field(description="Latchkey double reporting the signed-in account.")
    workspace_agent_id: str = Field(description="The test workspace's agent id.")
    permissions_path: Path = Field(description="The host permissions file the toggles edit.")
    carried_to_machines: list[str] = Field(
        default_factory=list,
        description="The workspace agent id of every edit pushed to a machine, in order.",
    )
    carried_with_desktop_egress_rules: list[str] = Field(
        default_factory=list,
        description="The workspace agent id of every push of the policy together with the rules file, in order.",
    )

    def apply_connector(self, scope: str, account: str, permission: str, enabled: bool) -> None:
        apply_connector_toggle(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            services_catalog=self.services_catalog,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            scope=scope,
            account=account,
            permission=permission,
            enabled=enabled,
            push_permissions_to_machine=self.carried_to_machines.append,
        )

    def apply_desktop_egress(self, service_name: str, hops: Sequence[str], device_id: str = _DEVICE_ID) -> None:
        apply_desktop_egress_route(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            services_catalog=self.services_catalog,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            service_name=service_name,
            route=build_desktop_egress_route(hops),
            device_id=device_id,
            push_permissions_and_desktop_egress_rules_to_machine=self.carried_with_desktop_egress_rules.append,
        )

    def put_this_computer_first(
        self, service_name: str, device_id: str = _DEVICE_ID
    ) -> DesktopEgressRouteThroughThisComputer:
        return put_this_computer_first_on_desktop_egress_route(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            services_catalog=self.services_catalog,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            service_name=service_name,
            device_id=device_id,
            push_permissions_and_desktop_egress_rules_to_machine=self.carried_with_desktop_egress_rules.append,
        )

    def apply_self(self, permission: str, enabled: bool) -> None:
        apply_self_toggle(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            permission=permission,
            enabled=enabled,
            push_permissions_to_machine=self.carried_to_machines.append,
        )


def _toggle_harness(tmp_path: Path, agent_id: AgentId, host: HostId) -> _ToggleHarness:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    return _ToggleHarness(
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)
        ),
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
        permissions_path=permissions_path_for_host(latchkey.plugin_data_dir, host),
    )


def test_apply_toggles_write_nothing_for_a_flip_that_changes_nothing(tmp_path: Path) -> None:
    """A flip to the state already stored must not touch the gateway at all.

    A rewrite that lands on the same content is not harmless: it is a write to a
    file the gateway shares with every other surface, and both apply functions
    promise not to make one.
    """
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    save_permissions(
        harness.permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_SHARED_PATH_PERMISSION]},),
            schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
        ),
    )
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    writes_so_far = len(harness.gateway_client.set_calls)

    # Already on, already off: neither direction has anything to store.
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-write", enabled=False)
    harness.apply_self(_SHARED_PATH_PERMISSION, True)
    harness.apply_self(f"minds-file-server-read-{_DEVICE_ID}:/Users/me/never-shared", False)

    assert len(harness.gateway_client.set_calls) == writes_so_far
    assert harness.gateway_client.deleted_rule_calls == ()


def test_apply_connector_toggle_writes_the_full_set_and_deletes_when_empty(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    rule_key = account_scope_key("slack-api", _ACCOUNT)

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({rule_key: ["slack-chat-read"]},)
    # The generated per-account schema travels with every write.
    assert rule_key in config.schemas

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-write-all", enabled=True)
    config = load_permissions(harness.permissions_path)
    # Full set, in catalog order -- never a diff.
    assert config.rules == ({rule_key: ["slack-write-all", "slack-chat-read"]},)

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=False)
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-write-all", enabled=False)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ()


def test_a_toggle_that_changed_the_file_hands_it_on_to_the_workspaces_machine(tmp_path: Path) -> None:
    """The machine's copy is what its gateway enforces, so every real edit has to travel."""
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    save_permissions(
        harness.permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
            schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
        ),
    )

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=False)

    assert harness.carried_to_machines == [harness.workspace_agent_id, harness.workspace_agent_id]


def test_a_toggle_that_changed_nothing_hands_on_nothing(tmp_path: Path) -> None:
    """Re-enabling what is already on writes no file, so there is no new policy to carry."""
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)
    carried_so_far = len(harness.carried_to_machines)

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-chat-read", enabled=True)

    assert len(harness.carried_to_machines) == carried_so_far


def test_apply_connector_toggle_rejects_unknown_scope(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    with pytest.raises(PermissionToggleError):
        harness.apply_connector(scope="nope-api", account=_ACCOUNT, permission="any", enabled=True)


def test_apply_self_toggle_rewrites_only_the_toggled_name(tmp_path: Path) -> None:
    harness = _toggle_harness(tmp_path, AgentId(), HostId())
    save_permissions(
        harness.permissions_path,
        LatchkeyPermissionsConfig(
            rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
            schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
        ),
    )

    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=False)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({SELF_SCOPE: [_BASELINE_PERMISSION]},)

    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=True)
    config = load_permissions(harness.permissions_path)
    assert config.rules == ({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},)


def _desktop_egress_harness(
    tmp_path: Path,
    host: HostId,
    is_machine_of_its_own: bool = True,
) -> _ToggleHarness:
    agent_id = AgentId()
    latchkey = _desktop_egress_latchkey(tmp_path)
    if is_machine_of_its_own:
        _give_host_a_machine_of_its_own(latchkey, host)
    return _ToggleHarness(
        backend_resolver=FixedHostBackendResolver(
            url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)
        ),
        gateway_client=build_fake_gateway_client(),
        services_catalog=_desktop_egress_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
        permissions_path=permissions_path_for_host(latchkey.plugin_data_dir, host),
    )


def _desktop_egress_grants(harness: _ToggleHarness) -> set[tuple[str, str]]:
    """``(scope, device_id)`` of every desktop egress grant in the host's file."""
    return {
        (grant.scope, grant.device_id)
        for grant in list_desktop_egress_grants(load_permissions(harness.permissions_path))
    }


def _desktop_egress_rules_copy(harness: _ToggleHarness, host: HostId) -> JsonValue:
    """The rules file as it is on disk, so the tests see the very shape the machine is sent."""
    return json.loads(desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).read_text())


def test_routing_a_service_through_this_computer_grants_it_every_scope_and_routes_the_service(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)

    harness.apply_desktop_egress("github", (_DEVICE_ID,))

    assert _desktop_egress_grants(harness) == {(scope, _DEVICE_ID) for scope in _GITHUB_SCOPES}
    assert _desktop_egress_rules_copy(harness, host) == {"github": [_DEVICE_ID]}
    rules_path = desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host)
    assert rules_path.stat().st_mode & 0o777 == 0o600
    # One push carries the policy and the rules file together, and the plain
    # policy push is not used.
    assert harness.carried_with_desktop_egress_rules == [harness.workspace_agent_id]
    assert harness.carried_to_machines == []


def test_setting_a_route_over_an_unusable_rules_copy_fails_and_changes_nothing(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    harness.apply_desktop_egress("github", (_DEVICE_ID,))
    harness.carried_with_desktop_egress_rules.clear()
    _write_desktop_egress_rules_copy(harness.latchkey, host, "[not rules")

    with pytest.raises(PermissionToggleError, match="Could not parse the desktop egress rules"):
        harness.apply_desktop_egress("slack", (_DEVICE_ID,))

    assert _desktop_egress_grants(harness) == {(scope, _DEVICE_ID) for scope in _GITHUB_SCOPES}
    rules_path = desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host)
    assert rules_path.read_text() == "[not rules"
    assert harness.carried_with_desktop_egress_rules == []


def test_a_route_through_two_desktops_grants_each_every_scope_and_stores_the_hops_in_order(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)

    harness.apply_desktop_egress("github", (_OTHER_DEVICE_ID, _DEVICE_ID, "self"))

    assert _desktop_egress_grants(harness) == {
        (scope, device_id) for scope in _GITHUB_SCOPES for device_id in (_DEVICE_ID, _OTHER_DEVICE_ID)
    }
    assert _desktop_egress_rules_copy(harness, host) == {"github": [_OTHER_DEVICE_ID, _DEVICE_ID, "self"]}
    assert harness.carried_with_desktop_egress_rules == [harness.workspace_agent_id]


def test_shrinking_a_route_deletes_the_grants_of_the_desktop_it_drops(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    harness.apply_desktop_egress("github", (_DEVICE_ID, _OTHER_DEVICE_ID))

    harness.apply_desktop_egress("github", (_OTHER_DEVICE_ID,))

    assert _desktop_egress_grants(harness) == {(scope, _OTHER_DEVICE_ID) for scope in _GITHUB_SCOPES}
    assert sorted(rule_key for _, rule_key in harness.gateway_client.deleted_rule_calls) == _egress_rule_keys(
        tuple((scope, _DEVICE_ID) for scope in _GITHUB_SCOPES)
    )
    assert _desktop_egress_rules_copy(harness, host) == {"github": [_OTHER_DEVICE_ID]}
    assert len(harness.carried_with_desktop_egress_rules) == 2


def test_routing_a_service_to_self_deletes_its_grants_and_keeps_the_routes_of_other_services(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    harness.apply_desktop_egress("slack", (_DEVICE_ID, "self"))
    harness.apply_desktop_egress("github", (_OTHER_DEVICE_ID,))
    assert _desktop_egress_rules_copy(harness, host) == {"github": [_OTHER_DEVICE_ID], "slack": [_DEVICE_ID, "self"]}

    harness.apply_desktop_egress("github", ("self",))

    assert _desktop_egress_grants(harness) == {("slack-api", _DEVICE_ID)}
    assert _desktop_egress_rules_copy(harness, host) == {"slack": [_DEVICE_ID, "self"]}

    harness.apply_desktop_egress("slack", ("self",))

    assert _desktop_egress_grants(harness) == set()
    assert _desktop_egress_rules_copy(harness, host) == {}
    assert len(harness.carried_with_desktop_egress_rules) == 4


def test_a_legacy_true_in_the_rules_copy_is_rewritten_as_the_route_its_grants_give(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    _write_desktop_egress_rules_copy(harness.latchkey, host, json.dumps({"github": True}))
    for scope in _GITHUB_SCOPES:
        _grant_desktop_egress(harness.latchkey, host, scope, _OTHER_DEVICE_ID)

    harness.apply_desktop_egress("slack", (_DEVICE_ID,))

    assert _desktop_egress_rules_copy(harness, host) == {"github": [_OTHER_DEVICE_ID], "slack": [_DEVICE_ID]}
    assert _desktop_egress_grants(harness) == {
        ("slack-api", _DEVICE_ID),
        *((scope, _OTHER_DEVICE_ID) for scope in _GITHUB_SCOPES),
    }
    assert harness.gateway_client.deleted_rule_calls == ()


def test_a_legacy_true_no_desktop_holds_every_scope_of_is_dropped_along_with_its_stray_grants(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    _write_desktop_egress_rules_copy(harness.latchkey, host, json.dumps({"github": True}))
    _grant_desktop_egress(harness.latchkey, host, "github-git", _OTHER_DEVICE_ID)

    harness.apply_desktop_egress("slack", (_DEVICE_ID,))

    assert _desktop_egress_rules_copy(harness, host) == {"slack": [_DEVICE_ID]}
    assert _desktop_egress_grants(harness) == {("slack-api", _DEVICE_ID)}


_UNSUPPORTED_WORKSPACE_CASES = [
    ("", True, "device id"),
    (_DEVICE_ID, False, "machine of its own"),
]


@pytest.mark.parametrize("device_id,is_machine_of_its_own,expected_fragment", _UNSUPPORTED_WORKSPACE_CASES)
@pytest.mark.parametrize("hops", [(_DEVICE_ID,), (_OTHER_DEVICE_ID, "self")])
def test_a_route_through_a_desktop_where_it_is_unsupported_is_refused_and_writes_nothing(
    tmp_path: Path,
    device_id: str,
    is_machine_of_its_own: bool,
    expected_fragment: str,
    hops: tuple[str, ...],
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host, is_machine_of_its_own=is_machine_of_its_own)

    with pytest.raises(PermissionToggleError, match=expected_fragment):
        harness.apply_desktop_egress("slack", hops, device_id=device_id)

    assert harness.gateway_client.set_calls == ()
    assert not desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).exists()
    assert harness.carried_with_desktop_egress_rules == []


@pytest.mark.parametrize(
    "device_id,is_machine_of_its_own",
    [(device_id, is_machine_of_its_own) for device_id, is_machine_of_its_own, _ in _UNSUPPORTED_WORKSPACE_CASES],
)
def test_routing_a_service_to_self_is_allowed_where_desktop_egress_is_unsupported(
    tmp_path: Path,
    device_id: str,
    is_machine_of_its_own: bool,
) -> None:
    """Whatever an earlier setup left behind can always be taken back."""
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host, is_machine_of_its_own=is_machine_of_its_own)
    _grant_desktop_egress(harness.latchkey, host, "slack-api", _DEVICE_ID)
    _write_desktop_egress_rules_copy(harness.latchkey, host, json.dumps({"slack": [_DEVICE_ID]}))

    harness.apply_desktop_egress("slack", ("self",), device_id=device_id)

    assert _desktop_egress_grants(harness) == set()
    assert _desktop_egress_rules_copy(harness, host) == {}
    assert harness.carried_with_desktop_egress_rules == [harness.workspace_agent_id]


def test_apply_desktop_egress_route_rejects_an_unknown_service(tmp_path: Path) -> None:
    harness = _desktop_egress_harness(tmp_path, HostId())

    with pytest.raises(PermissionToggleError, match="Unknown service 'not-a-service'"):
        harness.apply_desktop_egress("not-a-service", (_DEVICE_ID,))

    assert harness.carried_with_desktop_egress_rules == []


def test_apply_desktop_egress_route_rejects_a_workspace_it_cannot_resolve(tmp_path: Path) -> None:
    host = HostId()
    known_harness = _desktop_egress_harness(tmp_path, host)
    harness = known_harness.model_copy_update(to_update(known_harness.field_ref().workspace_agent_id, str(AgentId())))

    with pytest.raises(PermissionToggleError, match="Could not resolve host"):
        harness.apply_desktop_egress("slack", (_DEVICE_ID,))

    assert harness.gateway_client.set_calls == ()
    assert not desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).exists()
    assert harness.carried_with_desktop_egress_rules == []


class _MachineRefusedThePushError(Exception):
    """Stands in for whatever the push raises when the machine would not take the change."""


def _refuse_the_push(workspace_agent_id: str) -> None:
    raise _MachineRefusedThePushError(workspace_agent_id)


def test_apply_desktop_egress_route_lets_a_failed_push_propagate(tmp_path: Path) -> None:
    harness = _desktop_egress_harness(tmp_path, HostId())

    with pytest.raises(_MachineRefusedThePushError, match=harness.workspace_agent_id):
        apply_desktop_egress_route(
            backend_resolver=harness.backend_resolver,
            gateway_client=harness.gateway_client,
            services_catalog=harness.services_catalog,
            latchkey=harness.latchkey,
            workspace_agent_id=harness.workspace_agent_id,
            service_name="slack",
            route=build_desktop_egress_route((_DEVICE_ID,)),
            device_id=_DEVICE_ID,
            push_permissions_and_desktop_egress_rules_to_machine=_refuse_the_push,
        )


def _seed_github_desktop_egress(
    harness: _ToggleHarness, host: HostId, rule: JsonValue, granted_device_ids: Sequence[str]
) -> None:
    """Leave GitHub with ``rule`` in the rules copy and each of ``granted_device_ids`` holding its grants.

    Neither goes through the harness's gateway client, so the calls it records are those of the code under test.
    """
    for granted_device_id in granted_device_ids:
        for scope in _GITHUB_SCOPES:
            _grant_desktop_egress(harness.latchkey, host, scope, granted_device_id)
    _write_desktop_egress_rules_copy(harness.latchkey, host, json.dumps({"github": rule}))


def test_putting_this_computer_first_on_a_service_with_no_route_keeps_the_machine_as_what_is_tried_next(
    tmp_path: Path,
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)

    proxied = harness.put_this_computer_first("github")
    route = proxied.route

    assert proxied.is_this_computer_added is True
    assert route == build_desktop_egress_route((_DEVICE_ID, "self"))
    assert _desktop_egress_rules_copy(harness, host) == {"github": [_DEVICE_ID, "self"]}
    assert _desktop_egress_grants(harness) == {(scope, _DEVICE_ID) for scope in _GITHUB_SCOPES}
    assert harness.carried_with_desktop_egress_rules == [harness.workspace_agent_id]
    assert harness.carried_to_machines == []


@pytest.mark.parametrize(
    "rule,granted_device_ids,expected_hops",
    [
        pytest.param(["self"], (), (_DEVICE_ID, "self"), id="the_machine_itself"),
        pytest.param([_OTHER_DEVICE_ID], (_OTHER_DEVICE_ID,), (_DEVICE_ID, _OTHER_DEVICE_ID), id="another_desktop"),
        pytest.param(
            [_OTHER_DEVICE_ID, "self"],
            (_OTHER_DEVICE_ID,),
            (_DEVICE_ID, _OTHER_DEVICE_ID, "self"),
            id="another_desktop_then_the_machine",
        ),
        pytest.param(
            [_OTHER_DEVICE_ID, _THIRD_DEVICE_ID],
            (_OTHER_DEVICE_ID, _THIRD_DEVICE_ID),
            (_DEVICE_ID, _OTHER_DEVICE_ID, _THIRD_DEVICE_ID),
            id="two_other_desktops",
        ),
        pytest.param(
            True,
            (_OTHER_DEVICE_ID,),
            (_DEVICE_ID, _OTHER_DEVICE_ID),
            id="legacy_true_another_desktop_holds_the_grants_of",
        ),
        pytest.param(True, (), (_DEVICE_ID, "self"), id="legacy_true_no_desktop_holds_the_grants_of"),
    ],
)
def test_putting_this_computer_first_keeps_the_places_the_route_already_names_after_it(
    tmp_path: Path,
    rule: JsonValue,
    granted_device_ids: tuple[str, ...],
    expected_hops: tuple[str, ...],
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    _seed_github_desktop_egress(harness, host, rule, granted_device_ids)

    proxied = harness.put_this_computer_first("github")
    route = proxied.route

    assert proxied.is_this_computer_added is True
    assert route == build_desktop_egress_route(expected_hops)
    assert _desktop_egress_rules_copy(harness, host) == {"github": list(expected_hops)}
    assert _desktop_egress_grants(harness) == {
        (scope, device_id) for scope in _GITHUB_SCOPES for device_id in (_DEVICE_ID, *granted_device_ids)
    }
    assert harness.gateway_client.deleted_rule_calls == ()
    assert harness.carried_with_desktop_egress_rules == [harness.workspace_agent_id]


@pytest.mark.parametrize(
    "rule,granted_device_ids,expected_hops",
    [
        pytest.param([_DEVICE_ID], (_DEVICE_ID,), (_DEVICE_ID,), id="named_alone"),
        pytest.param([_DEVICE_ID, "self"], (_DEVICE_ID,), (_DEVICE_ID, "self"), id="named_first_before_the_machine"),
        pytest.param(
            [_DEVICE_ID, _OTHER_DEVICE_ID],
            (_DEVICE_ID, _OTHER_DEVICE_ID),
            (_DEVICE_ID, _OTHER_DEVICE_ID),
            id="named_first_before_another_desktop",
        ),
        pytest.param(
            [_OTHER_DEVICE_ID, _DEVICE_ID],
            (_OTHER_DEVICE_ID, _DEVICE_ID),
            (_OTHER_DEVICE_ID, _DEVICE_ID),
            id="named_after_another_desktop",
        ),
        pytest.param(
            [_OTHER_DEVICE_ID, _DEVICE_ID, "self"],
            (_OTHER_DEVICE_ID, _DEVICE_ID),
            (_OTHER_DEVICE_ID, _DEVICE_ID, "self"),
            id="named_between_another_desktop_and_the_machine",
        ),
        pytest.param(True, (_DEVICE_ID,), (_DEVICE_ID,), id="legacy_true_this_computer_holds_the_grants_of"),
        pytest.param(
            True,
            (_OTHER_DEVICE_ID, _DEVICE_ID),
            (_OTHER_DEVICE_ID, _DEVICE_ID),
            id="legacy_true_this_computer_holds_the_grants_of_after_another_desktop",
        ),
    ],
)
def test_putting_this_computer_first_on_a_route_that_already_names_it_returns_the_route_and_writes_nothing(
    tmp_path: Path,
    rule: JsonValue,
    granted_device_ids: tuple[str, ...],
    expected_hops: tuple[str, ...],
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    _seed_github_desktop_egress(harness, host, rule, granted_device_ids)
    rules_path = desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host)
    rules_before = rules_path.read_bytes()
    permissions_before = harness.permissions_path.read_bytes()

    proxied = harness.put_this_computer_first("github")
    route = proxied.route

    assert proxied.is_this_computer_added is False
    assert route == build_desktop_egress_route(expected_hops)
    # The seeded copy is not laid out the way a written one is, so a rewrite of the same routes would show here.
    assert rules_path.read_bytes() == rules_before
    assert harness.permissions_path.read_bytes() == permissions_before
    assert harness.gateway_client.set_calls == ()
    assert harness.gateway_client.deleted_rule_calls == ()
    assert harness.carried_with_desktop_egress_rules == []


def test_put_this_computer_first_on_desktop_egress_route_rejects_an_unknown_service(tmp_path: Path) -> None:
    harness = _desktop_egress_harness(tmp_path, HostId())

    with pytest.raises(PermissionToggleError, match="Unknown service 'not-a-service'"):
        harness.put_this_computer_first("not-a-service")

    assert harness.carried_with_desktop_egress_rules == []


def test_put_this_computer_first_on_desktop_egress_route_rejects_a_workspace_it_cannot_resolve(
    tmp_path: Path,
) -> None:
    host = HostId()
    known_harness = _desktop_egress_harness(tmp_path, host)
    harness = known_harness.model_copy_update(to_update(known_harness.field_ref().workspace_agent_id, str(AgentId())))

    with pytest.raises(PermissionToggleError, match="Could not resolve host"):
        harness.put_this_computer_first("slack")

    assert harness.gateway_client.set_calls == ()
    assert not desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).exists()
    assert harness.carried_with_desktop_egress_rules == []


@pytest.mark.parametrize("device_id,is_machine_of_its_own,expected_fragment", _UNSUPPORTED_WORKSPACE_CASES)
def test_putting_this_computer_first_where_desktop_egress_is_unsupported_is_refused_and_writes_nothing(
    tmp_path: Path,
    device_id: str,
    is_machine_of_its_own: bool,
    expected_fragment: str,
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host, is_machine_of_its_own=is_machine_of_its_own)

    with pytest.raises(PermissionToggleError, match=expected_fragment):
        harness.put_this_computer_first("slack", device_id=device_id)

    assert harness.gateway_client.set_calls == ()
    assert not desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).exists()
    assert harness.carried_with_desktop_egress_rules == []


@pytest.mark.parametrize(
    "rules_json",
    [
        pytest.param("[not rules", id="not_json"),
        pytest.param(json.dumps([_OTHER_DEVICE_ID]), id="not_an_object"),
        pytest.param(json.dumps({"slack": _OTHER_DEVICE_ID}), id="a_rule_that_is_not_a_list_of_hops"),
        pytest.param(json.dumps({"github": ["self", _OTHER_DEVICE_ID]}), id="another_services_rule_that_is_no_route"),
    ],
)
def test_putting_this_computer_first_over_an_unusable_rules_copy_fails_and_changes_nothing(
    tmp_path: Path, rules_json: str
) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    _write_desktop_egress_rules_copy(harness.latchkey, host, rules_json)

    with pytest.raises(PermissionToggleError, match="Could not parse the desktop egress rules"):
        harness.put_this_computer_first("slack")

    assert harness.gateway_client.set_calls == ()
    rules_path = desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host)
    assert rules_path.read_text() == rules_json
    assert harness.carried_with_desktop_egress_rules == []


def test_putting_first_a_computer_whose_device_id_is_no_hop_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)

    with pytest.raises(PermissionToggleError, match="Could not put this computer on the route"):
        harness.put_this_computer_first("slack", device_id=f" {_DEVICE_ID}")

    assert harness.gateway_client.set_calls == ()
    assert not desktop_egress_rules_path_for_host(harness.latchkey.plugin_data_dir, host).exists()
    assert harness.carried_with_desktop_egress_rules == []


def test_put_this_computer_first_on_desktop_egress_route_lets_a_failed_push_propagate(tmp_path: Path) -> None:
    harness = _desktop_egress_harness(tmp_path, HostId())

    with pytest.raises(_MachineRefusedThePushError, match=harness.workspace_agent_id):
        put_this_computer_first_on_desktop_egress_route(
            backend_resolver=harness.backend_resolver,
            gateway_client=harness.gateway_client,
            services_catalog=harness.services_catalog,
            latchkey=harness.latchkey,
            workspace_agent_id=harness.workspace_agent_id,
            service_name="slack",
            device_id=_DEVICE_ID,
            push_permissions_and_desktop_egress_rules_to_machine=_refuse_the_push,
        )


def test_account_and_self_toggles_and_a_revoke_leave_desktop_egress_grants_in_place(tmp_path: Path) -> None:
    """The machine's gateway checks the account before it routes, so a grant left behind allows nothing."""
    host = HostId()
    harness = _desktop_egress_harness(tmp_path, host)
    harness.apply_desktop_egress("slack", (_DEVICE_ID,))
    harness.gateway_client.set_permission_rule(
        harness.permissions_path,
        SELF_SCOPE,
        [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION],
        schemas={_SHARED_PATH_PERMISSION: {"type": "object"}},
    )

    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-read-all", enabled=True)
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-read-all", enabled=False)
    harness.apply_self(permission=_SHARED_PATH_PERMISSION, enabled=False)
    harness.apply_connector(scope="slack-api", account=_ACCOUNT, permission="slack-read-all", enabled=True)
    revoke_service_account_for_workspace(
        backend_resolver=harness.backend_resolver,
        gateway_client=harness.gateway_client,
        services_catalog=harness.services_catalog,
        latchkey=harness.latchkey,
        workspace_agent_id=harness.workspace_agent_id,
        service_name="slack",
        account=_ACCOUNT,
        push_permissions_to_machine=harness.carried_to_machines.append,
    )

    assert _desktop_egress_grants(harness) == {("slack-api", _DEVICE_ID)}
    assert _desktop_egress_rules_copy(harness, host) == {"slack": [_DEVICE_ID]}
    # The account's own grant is gone, which is what the revoke is for.
    assert [
        grant.account
        for grant in harness.services_catalog.list_service_account_grants(load_permissions(harness.permissions_path))
    ] == []


def _connect_aws(
    latchkey: FakeAccountsLatchkey,
    value_by_parameter_name: dict[str, str] | None = None,
    account_name: str = "",
) -> str:
    return connect_service_with_credentials(
        latchkey=latchkey,
        services_catalog=build_permissions_test_catalog(),
        service_name="aws",
        value_by_parameter_name=(
            value_by_parameter_name
            if value_by_parameter_name is not None
            else {"access-key-id": "AKIAEXAMPLE", "secret-access-key": "s3cret"}
        ),
        account_name=account_name,
    )


def test_connect_service_with_credentials_runs_the_service_own_command(tmp_path: Path) -> None:
    """The values fill the service's own example, pinned to the account they create."""
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    stored_account = _connect_aws(latchkey)

    assert latchkey.auth_set_calls == [
        ("aws", ("--account", "", "auth", "set-nocurl", "aws", "AKIAEXAMPLE", "s3cret")),
    ]
    # The stored credentials are the service's first account, so it now connects.
    assert latchkey.accounts_by_service == {"aws": [""]}
    # The first account is latchkey's unnamed default, and that is what is reported.
    assert stored_account == ""


def test_connect_service_with_credentials_names_a_further_account(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": [""]},
    )

    stored_account = _connect_aws(latchkey, account_name="  work  ")

    assert latchkey.auth_set_calls[0][1][:2] == ("--account", "work")
    # Reported back so the caller can hand exactly this account to the machine.
    assert stored_account == "work"


def test_connect_service_with_credentials_requires_a_name_for_a_further_account(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": [""]},
    )

    with pytest.raises(PermissionToggleError, match="Enter a name for the new AWS account"):
        _connect_aws(latchkey)
    assert latchkey.auth_set_calls == []


def test_connect_service_with_credentials_rejects_a_blank_value(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    with pytest.raises(PermissionToggleError, match="Secret access key"):
        _connect_aws(latchkey, {"access-key-id": "AKIAEXAMPLE", "secret-access-key": "  "})
    assert latchkey.auth_set_calls == []


def test_connect_service_with_credentials_reports_what_the_service_refused(tmp_path: Path) -> None:
    """Latchkey's own explanation is kept; its usage lines and stack frames are not."""
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        auth_set_result=(
            False,
            "Error: that does not look like an AWS access key ID\nExample: latchkey auth set-nocurl aws <id> <key>",
        ),
    )

    with pytest.raises(PermissionToggleError) as failure:
        _connect_aws(latchkey)

    assert str(failure.value) == "AWS rejected those credentials: that does not look like an AWS access key ID"
    # Nothing was stored, so the pane still offers AWS under Add connection.
    assert latchkey.accounts_by_service == {}


def test_connect_service_with_credentials_rejects_a_browser_service(tmp_path: Path) -> None:
    """Slack signs in through a browser, so it takes no credentials from this route."""
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    with pytest.raises(PermissionToggleError, match="signing in"):
        connect_service_with_credentials(
            latchkey=latchkey,
            services_catalog=build_permissions_test_catalog(),
            service_name="slack",
            value_by_parameter_name={"token": "t"},
            account_name="",
        )
    assert latchkey.auth_set_calls == []


def test_connect_service_with_credentials_says_so_when_the_probe_did_not_report(tmp_path: Path) -> None:
    """A probe that failed (``None``) must not be guessed around.

    Without an answer there is no way to tell a credentials service from a
    browser one -- guessing "browser" would tell the user who just typed an
    AWS key that they picked the wrong connection method and throw away
    everything they typed.
    """

    class _UnreachableProbeLatchkey(FakeAccountsLatchkey):
        def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
            del service_name, is_offline
            return None

    latchkey = _UnreachableProbeLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    with pytest.raises(PermissionToggleError, match="could not ask latchkey how AWS connects"):
        _connect_aws(latchkey)
    assert latchkey.auth_set_calls == []


def test_connect_service_with_credentials_reads_the_probe_offline(tmp_path: Path) -> None:
    """Nothing this route reads needs network validation, and the user is waiting on it."""
    probe_calls: list[bool] = []

    class _RecordingProbeLatchkey(FakeAccountsLatchkey):
        def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo | None:
            probe_calls.append(is_offline)
            return super().services_info(service_name, is_offline=is_offline)

    latchkey = _RecordingProbeLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    _connect_aws(latchkey)

    assert probe_calls == [True]


def test_connect_service_with_credentials_rejects_an_unknown_service(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")

    with pytest.raises(PermissionToggleError, match="Unknown service"):
        connect_service_with_credentials(
            latchkey=latchkey,
            services_catalog=build_permissions_test_catalog(),
            service_name="not-a-service",
            value_by_parameter_name={},
            account_name="",
        )


def test_connect_service_with_credentials_refuses_an_unusable_command(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        credential_example_by_service={"aws": "latchkey auth set-nocurl aws"},
    )

    with pytest.raises(PermissionToggleError, match="cannot work out which credentials"):
        _connect_aws(latchkey)
    assert latchkey.auth_set_calls == []


def test_a_custom_services_own_header_is_what_the_pane_collects_and_stores(tmp_path: Path) -> None:
    """latchkey reports a bearer command for every generic registered service, so the
    header a custom service was registered with has to come off the registration or the
    typed token is stored under Authorization instead of the header the service takes."""
    service_name = custom_service_name("api.example.com", "https")
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        credential_example_by_service={
            service_name: f'latchkey auth set {service_name} -H "Authorization: Bearer <token>"'
        },
    )
    latchkey.register_custom_service(
        service_name,
        build_custom_service_registration("api.example.com", "https", credential_header="X-Api-Key: {token}"),
    )
    catalog = ServicesCatalog(latchkey_directory=tmp_path)

    stored_account = connect_service_with_credentials(
        latchkey=latchkey,
        services_catalog=catalog,
        service_name=service_name,
        value_by_parameter_name={"token": "k-42"},
        account_name="",
    )

    assert stored_account == DEFAULT_ACCOUNT
    assert latchkey.auth_set_calls[0][1][-2:] == ("-H", "X-Api-Key: k-42")
