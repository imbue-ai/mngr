"""Unit tests for the per-workspace permission-toggle module."""

from pathlib import Path

import pytest
from pydantic import Field
from pydantic import JsonValue

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.latchkey.permission_overview import SELF_SCOPE
from imbue.minds.desktop_client.latchkey.permission_toggles import PermissionToggleError
from imbue.minds.desktop_client.latchkey.permission_toggles import WorkspacePermissionsView
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_connector_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import apply_self_toggle
from imbue.minds.desktop_client.latchkey.permission_toggles import build_file_sharing_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_permissions_view
from imbue.minds.desktop_client.latchkey.permission_toggles import build_workspace_toggles
from imbue.minds.desktop_client.latchkey.permission_toggles import classify_permission
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_connector_permissions
from imbue.minds.desktop_client.latchkey.permission_toggles import compute_self_permissions
from imbue.minds.desktop_client.latchkey.permission_toggles import connect_service_with_credentials
from imbue.minds.desktop_client.latchkey.testing import FakeAccountsLatchkey
from imbue.minds.desktop_client.latchkey.testing import FakeLatchkeyGatewayClient
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.latchkey.testing import build_permissions_test_catalog
from imbue.minds.desktop_client.latchkey.testing import seed_connector_grant
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import account_scope_key
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import DEFAULT_ACCOUNT
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.services_catalog import ServicePermissionInfo
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import load_permissions
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions
from imbue.mngr_latchkey.workspace_permissions import WORKSPACE_VERBS

_ACCOUNT = "alice@example.com"


class _HostResolver(StaticBackendResolver):
    """Static resolver mapping every known agent to one fixed host."""

    fixed_host_id: HostId = Field(description="Host id reported for every known agent.")
    known_agent_ids: tuple[AgentId, ...] = Field(default=())

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return self.known_agent_ids

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        if agent_id not in self.known_agent_ids:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=str(self.fixed_host_id))


def _slack_info() -> ServicePermissionInfo:
    info = build_permissions_test_catalog().get_by_scope("slack-api")
    assert info is not None
    return info


# -- classify_permission -------------------------------------------------------


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


# -- compute_connector_permissions ---------------------------------------------


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


# -- compute_self_permissions --------------------------------------------------


_SHARED_PATH_PERMISSION = "minds-file-server-read-/Users/me/notes"
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


# -- latchkey-self toggle rows -------------------------------------------------


def test_build_file_sharing_toggles_includes_revoked_but_restorable_paths() -> None:
    """A path whose schema is still in the file renders as an off toggle that can be re-enabled."""
    write_permission = "minds-file-server-write-/Users/me/notes"
    config = LatchkeyPermissionsConfig(
        rules=({SELF_SCOPE: [_BASELINE_PERMISSION, _SHARED_PATH_PERMISSION]},),
        schemas={_SHARED_PATH_PERMISSION: {"type": "object"}, write_permission: {"type": "object"}},
    )
    toggles = build_file_sharing_toggles(config)
    assert [(toggle.permission, toggle.is_granted, toggle.can_enable) for toggle in toggles] == [
        (_SHARED_PATH_PERMISSION, True, True),
        (write_permission, False, True),
    ]
    assert toggles[0].label == "/Users/me/notes"
    assert toggles[0].detail == "read"
    assert toggles[1].detail == "read and write"


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

    file_sharing = build_file_sharing_toggles(config)
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


# -- build_workspace_permissions_view ------------------------------------------


def test_build_workspace_permissions_view_marks_granted_toggles(tmp_path: Path) -> None:
    agent_id, host = AgentId(), HostId()
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    seed_connector_grant(latchkey.plugin_data_dir, host, "slack-api", _ACCOUNT, ("slack-chat-read",))
    resolver = _HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
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
    resolver = _HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,))

    view = build_workspace_permissions_view(
        backend_resolver=resolver,
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
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


def _build_view(latchkey: FakeAccountsLatchkey, agent_id: AgentId, host: HostId) -> WorkspacePermissionsView:
    return build_workspace_permissions_view(
        backend_resolver=_HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)),
        gateway_client=build_fake_gateway_client(),
        services_catalog=build_permissions_test_catalog(),
        latchkey=latchkey,
        workspace_agent_id=str(agent_id),
    )


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
            workspace_agent_id=str(AgentId()),
        )


# -- apply_connector_toggle / apply_self_toggle --------------------------------


class _ToggleHarness(FrozenModel):
    """The typed dependency bundle the apply_* functions take, plus the host file path."""

    backend_resolver: _HostResolver = Field(description="Resolver mapping the test agent to its host.")
    gateway_client: FakeLatchkeyGatewayClient = Field(description="Fake gateway writing a real on-disk file.")
    services_catalog: ServicesCatalog = Field(description="Catalog built from the test payload.")
    latchkey: FakeAccountsLatchkey = Field(description="Latchkey double reporting the signed-in account.")
    workspace_agent_id: str = Field(description="The test workspace's agent id.")
    permissions_path: Path = Field(description="The host permissions file the toggles edit.")

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
        )

    def apply_self(self, permission: str, enabled: bool) -> None:
        apply_self_toggle(
            backend_resolver=self.backend_resolver,
            gateway_client=self.gateway_client,
            latchkey=self.latchkey,
            workspace_agent_id=self.workspace_agent_id,
            permission=permission,
            enabled=enabled,
        )


def _toggle_harness(tmp_path: Path, agent_id: AgentId, host: HostId) -> _ToggleHarness:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_ACCOUNT]},
    )
    return _ToggleHarness(
        backend_resolver=_HostResolver(url_by_agent_and_service={}, fixed_host_id=host, known_agent_ids=(agent_id,)),
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
    harness.apply_self("minds-file-server-read-/Users/me/never-shared", False)

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


# -- connect_service_with_credentials ------------------------------------------


def _connect_aws(
    latchkey: FakeAccountsLatchkey,
    value_by_parameter_name: dict[str, str] | None = None,
    account_name: str = "",
) -> None:
    connect_service_with_credentials(
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

    _connect_aws(latchkey)

    assert latchkey.auth_set_calls == [
        ("aws", ("--account", "", "auth", "set-nocurl", "aws", "AKIAEXAMPLE", "s3cret")),
    ]
    # The stored credentials are the service's first account, so it now connects.
    assert latchkey.accounts_by_service == {"aws": [""]}


def test_connect_service_with_credentials_names_a_further_account(tmp_path: Path) -> None:
    latchkey = FakeAccountsLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"aws": [""]},
    )

    _connect_aws(latchkey, account_name="  work  ")

    assert latchkey.auth_set_calls[0][1][:2] == ("--account", "work")


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
