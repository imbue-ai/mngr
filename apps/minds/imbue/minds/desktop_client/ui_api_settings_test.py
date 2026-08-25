import json
from pathlib import Path

from pydantic import AnyUrl
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.latchkey.handlers.messaging import MngrMessageSender
from imbue.minds.desktop_client.latchkey.handlers.predefined import LatchkeyPermissionGrantHandler
from imbue.minds.desktop_client.latchkey.testing import build_fake_gateway_client
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.testing import WriteCountingMindsConfig
from imbue.minds.desktop_client.ui_api_settings import compute_error_reporting_version
from imbue.minds.desktop_client.ui_api_settings import compute_notification_prefs_version
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId
from imbue.mngr_latchkey.account_scopes import build_account_grant
from imbue.mngr_latchkey.core import CredentialStatus
from imbue.mngr_latchkey.core import Latchkey
from imbue.mngr_latchkey.core import LatchkeyServiceInfo
from imbue.mngr_latchkey.core import ServiceAccountCredential
from imbue.mngr_latchkey.services_catalog import ServicesCatalog
from imbue.mngr_latchkey.store import LatchkeyPermissionsConfig
from imbue.mngr_latchkey.store import permissions_path_for_host
from imbue.mngr_latchkey.store import save_permissions


def test_settings_overview_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/settings")

    assert response.status_code == 401


def test_settings_overview_returns_empty_permissions_and_a_version_without_a_latchkey_handler(
    tmp_path: Path,
) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["services_overview"] == []
    assert payload["file_sharing_grants"] == []
    assert payload["workspace_delegation_grants"] == []
    assert payload["permissions_unavailable"] is False
    assert payload["report_unexpected_errors"] is True
    assert payload["version"] == compute_error_reporting_version(True)


def test_error_reporting_write_round_trips_with_the_served_version(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    served_version = json.loads(client.get("/ui/api/settings").data)["version"]

    response = client.post(
        "/ui/api/settings/error-reporting",
        json={"report_unexpected_errors": False},
        headers={"If-Match": served_version},
    )

    assert response.status_code == 200
    assert json.loads(response.data)["version"] == compute_error_reporting_version(False)
    assert minds_config.get_report_unexpected_errors() is False
    # The write must reach the detached latchkey forward daemon's live consent
    # file too, so the opt-out takes effect without an app restart.
    consent_path = latchkey_forward_sentry_consent_path(minds_config.data_dir)
    assert json.loads(consent_path.read_text())["report_unexpected_errors"] is False


def test_error_reporting_write_with_a_malformed_body_is_rejected_with_400(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    served_version = json.loads(client.get("/ui/api/settings").data)["version"]

    response = client.post(
        "/ui/api/settings/error-reporting",
        json={"report_unexpected_errors": "yes please"},
        headers={"If-Match": served_version},
    )

    assert response.status_code == 400
    assert minds_config.get_report_unexpected_errors() is True


def test_error_reporting_write_with_a_stale_version_is_rejected_with_412(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    stale_version = compute_error_reporting_version(True)
    # Another window flips the flag after this page loaded its version.
    minds_config.set_report_unexpected_errors(False)

    response = client.post(
        "/ui/api/settings/error-reporting",
        json={"report_unexpected_errors": True},
        headers={"If-Match": stale_version},
    )

    assert response.status_code == 412
    # The stale write must not have clobbered the newer value.
    assert minds_config.get_report_unexpected_errors() is False


def test_error_reporting_write_without_if_match_is_rejected_with_428(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/settings/error-reporting", json={"report_unexpected_errors": False})

    assert response.status_code == 428
    assert minds_config.get_report_unexpected_errors() is True


def test_settings_overview_carries_the_default_notification_prefs_with_their_own_version(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    assert json.loads(response.data)["notification_prefs"] == {
        "is_enabled": True,
        "style": "both",
        "is_os_hint_dismissed": False,
        "os_permission_confirmed": False,
        "version": compute_notification_prefs_version(is_enabled=True, style="both", is_os_hint_dismissed=False),
    }


def test_settings_overview_serves_default_notification_prefs_without_a_minds_config(tmp_path: Path) -> None:
    """The degraded (no MindsConfig) overview still carries the record, at its defaults."""
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    prefs = json.loads(response.data)["notification_prefs"]
    assert prefs["is_enabled"] is True
    assert prefs["style"] == "both"
    assert prefs["is_os_hint_dismissed"] is False


def test_notification_prefs_write_round_trips_with_the_served_version(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    served_version = json.loads(client.get("/ui/api/settings").data)["notification_prefs"]["version"]

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": False, "style": "cards", "is_os_hint_dismissed": True},
        headers={"If-Match": served_version},
    )

    assert response.status_code == 200
    new_version = compute_notification_prefs_version(is_enabled=False, style="cards", is_os_hint_dismissed=True)
    assert json.loads(response.data)["version"] == new_version
    assert minds_config.get_notification_prefs()[:3] == (False, "cards", True)
    # The next overview serves the written values under the new version.
    assert json.loads(client.get("/ui/api/settings").data)["notification_prefs"]["version"] == new_version


def test_notification_prefs_write_lands_all_three_values_in_one_config_write(tmp_path: Path) -> None:
    """The route persists the record through one atomic read-modify-write.

    Three separate setter calls would open a window where a concurrent writer
    interleaves into a record mixing one writer's toggle with the other's
    style; a single write means every stored record is exactly one request's.
    """
    minds_config = WriteCountingMindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    served_version = json.loads(client.get("/ui/api/settings").data)["notification_prefs"]["version"]

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": False, "style": "os", "is_os_hint_dismissed": True},
        headers={"If-Match": served_version},
    )

    assert response.status_code == 200
    assert minds_config.write_count == 1
    assert minds_config.get_notification_prefs()[:3] == (False, "os", True)


def test_notification_prefs_write_with_a_malformed_style_is_rejected_with_400(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    served_version = json.loads(client.get("/ui/api/settings").data)["notification_prefs"]["version"]

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": True, "style": "shout", "is_os_hint_dismissed": False},
        headers={"If-Match": served_version},
    )

    assert response.status_code == 400
    assert minds_config.get_notification_prefs()[1] == "both"


def test_notification_prefs_write_with_a_stale_version_is_rejected_with_412(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    stale_version = compute_notification_prefs_version(is_enabled=True, style="both", is_os_hint_dismissed=False)
    # Another window changes the prefs after this page loaded its version.
    minds_config.set_notification_prefs(is_enabled=True, style=NotificationStyle.OS, is_os_hint_dismissed=False)

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": True, "style": "cards", "is_os_hint_dismissed": False},
        headers={"If-Match": stale_version},
    )

    assert response.status_code == 412
    # The stale write must not have clobbered the newer value.
    assert minds_config.get_notification_prefs()[1] == "os"


def test_notification_prefs_write_without_if_match_is_rejected_with_428(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": False, "style": "both", "is_os_hint_dismissed": False},
    )

    assert response.status_code == 428
    assert minds_config.get_notification_prefs()[0] is True


def test_notification_os_permission_write_persists_the_confirmed_flag(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post(
        "/ui/api/settings/notification-os-permission",
        json={"os_permission_confirmed": True},
    )

    assert response.status_code == 204
    assert minds_config.get_notification_prefs()[3] is True
    assert json.loads(client.get("/ui/api/settings").data)["notification_prefs"]["os_permission_confirmed"] is True


def test_notification_os_permission_write_requires_no_if_match(tmp_path: Path) -> None:
    """Unlike the guarded prefs write: this is system-observed state, not a user
    preference, so there is nothing for a stale window to clobber."""
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post(
        "/ui/api/settings/notification-os-permission",
        json={"os_permission_confirmed": False},
    )

    assert response.status_code == 204


def test_notification_os_permission_write_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.post(
        "/ui/api/settings/notification-os-permission",
        json={"os_permission_confirmed": True},
    )

    assert response.status_code == 401


def test_notification_os_permission_write_rejects_a_malformed_body(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/settings/notification-os-permission", json={"confirmed": "yes"})

    assert response.status_code == 400


def test_notification_prefs_write_without_a_minds_config_is_rejected_with_503(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.post(
        "/ui/api/settings/notifications",
        json={"is_enabled": False, "style": "both", "is_os_hint_dismissed": False},
        headers={"If-Match": "anything"},
    )

    assert response.status_code == 503


def test_malformed_stored_notification_style_serves_the_default_on_the_overview(tmp_path: Path) -> None:
    """A malformed on-disk style must degrade to the default, not break the settings page."""
    config_dir = tmp_path / "config"
    minds_config = MindsConfig(data_dir=config_dir)
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "config.toml").write_text('notification_style = "shout"\n')

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    assert json.loads(response.data)["notification_prefs"]["style"] == "both"


def test_accounts_detail_returns_an_empty_list_without_a_session_store(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/accounts")

    assert response.status_code == 200
    assert json.loads(response.data) == {"accounts": []}


def test_account_plan_degrades_to_null_plan_view_without_a_connector(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/accounts/user-123/plan")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["plan_view"] is None
    assert payload["trim_status"] is None
    # No client env config means no known origin for the privacy policy.
    assert payload["privacy_policy_url"] == ""


def test_account_plan_resolves_the_privacy_policy_url_from_the_client_env_config(tmp_path: Path) -> None:
    """The Learn-more link prefers the accounts origin and falls back to the connector host."""
    connector_only = ClientEnvConfig(
        connector_url=AnyUrl("https://connector.example.com"),
        litellm_proxy_url=AnyUrl("https://llm.example.com"),
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, client_env_config=connector_only
    )
    payload = json.loads(client.get("/ui/api/accounts/user-123/plan").data)
    assert payload["privacy_policy_url"] == "https://connector.example.com/privacy-policy"

    with_accounts_origin = ClientEnvConfig(
        connector_url=AnyUrl("https://connector.example.com"),
        litellm_proxy_url=AnyUrl("https://llm.example.com"),
        accounts_base_url=AnyUrl("https://accounts.example.com"),
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path / "accounts-origin", is_authenticated=True, client_env_config=with_accounts_origin
    )
    payload = json.loads(client.get("/ui/api/accounts/user-123/plan").data)
    assert payload["privacy_policy_url"] == "https://accounts.example.com/privacy-policy"


def test_ai_keys_context_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/ai-keys")

    assert response.status_code == 401


def test_ai_keys_context_explains_when_no_workspace_is_given(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/ai-keys")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["workspace_host_id"] == ""
    assert "opened from a machine" in payload["error_message"]


def test_ai_keys_context_reports_a_missing_account_association(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/ai-keys?workspace=host-00000000000000000000000000000abc")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["workspace_host_id"] == "host-00000000000000000000000000000abc"
    assert "no associated Imbue account" in payload["error_message"]


# -- Populated permissions overview (mirrors the deleted settings_routes_test.py coverage) --

_CONNECTOR_CATALOG_PAYLOAD: dict[str, object] = {
    "slack": [
        {
            "scope": "slack-api",
            "display_name": "Slack",
            "permissions": [
                {"name": "slack-read-all", "description": "All read operations across the Slack API."},
                {"name": "slack-write-all"},
            ],
        },
    ],
}

# The signed-in account the connector fixtures grant permissions to.
_TEST_ACCOUNT: str = "hynek@imbue-ai"


class _ConnectorLatchkey(Latchkey):
    """Non-spawning ``Latchkey`` double reporting configurable stored accounts.

    ``auth_list`` is what ``build_permission_overview`` reads to know which
    accounts have stored credentials (the ``is_connected`` flag).
    """

    accounts_by_service: dict[str, list[str]] = Field(default_factory=dict)

    def _accounts_for(self, service_name: str) -> tuple[ServiceAccountCredential, ...]:
        return tuple(
            ServiceAccountCredential(account=account, credential_status=CredentialStatus.VALID)
            for account in self.accounts_by_service.get(service_name, [])
        )

    def services_info(self, service_name: str, *, is_offline: bool = False) -> LatchkeyServiceInfo:
        del is_offline
        accounts = self._accounts_for(service_name)
        return LatchkeyServiceInfo(
            credential_status=CredentialStatus.VALID if accounts else CredentialStatus.MISSING,
            accounts=accounts,
            auth_options=frozenset({"browser", "set"}),
            set_credentials_example=None,
        )

    def auth_list(self, *, is_offline: bool = False) -> dict[str, tuple[ServiceAccountCredential, ...]]:
        del is_offline
        return {service: self._accounts_for(service) for service in self.accounts_by_service}


class _WorkspaceResolver(StaticBackendResolver):
    """Static resolver that reports active machines mapped to hosts, with names."""

    host_by_agent: dict[str, str] = Field(default_factory=dict)
    name_by_agent: dict[str, str] = Field(default_factory=dict)

    def list_known_agent_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(a) for a in self.host_by_agent)

    def list_active_workspace_ids(self) -> tuple[AgentId, ...]:
        return tuple(AgentId(a) for a in self.host_by_agent)

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host = self.host_by_agent.get(str(agent_id))
        if host is None:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host)

    def get_workspace_name(self, agent_id: AgentId) -> str | None:
        return self.name_by_agent.get(str(agent_id))


def _build_connector_grant_handler(tmp_path: Path, latchkey: Latchkey) -> LatchkeyPermissionGrantHandler:
    return LatchkeyPermissionGrantHandler(
        data_dir=tmp_path,
        latchkey=latchkey,
        services_catalog=ServicesCatalog.from_catalog_payload(_CONNECTOR_CATALOG_PAYLOAD),
        mngr_message_sender=MngrMessageSender(
            mngr_caller=RecordingMngrCaller(),
            # The settings overview never sends messages; an un-entered group
            # satisfies the required field.
            concurrency_group=ConcurrencyGroup(name="ui-api-settings-test-unused"),
        ),
        gateway_client=build_fake_gateway_client(),
    )


def _seed_slack_grant(latchkey: Latchkey, host_id: HostId, account: str, permissions: tuple[str, ...]) -> None:
    """Write the per-host permissions file production writes for a Slack grant to ``account``."""
    rule_key, granted, schemas = build_account_grant("slack-api", account, permissions)
    save_permissions(
        permissions_path_for_host(latchkey.plugin_data_dir, host_id),
        LatchkeyPermissionsConfig(rules=({rule_key: list(granted)},), schemas=schemas),
    )


def test_settings_overview_lists_a_granted_connector_with_workspace_and_permissions(tmp_path: Path) -> None:
    """A granted connector shows up in the settings JSON with its machine and permission label."""
    agent, host = str(AgentId()), HostId()
    latchkey = _ConnectorLatchkey(
        latchkey_directory=tmp_path,
        latchkey_binary="/nonexistent",
        accounts_by_service={"slack": [_TEST_ACCOUNT]},
    )
    _seed_slack_grant(latchkey, host, _TEST_ACCOUNT, ("slack-read-all",))
    handler = _build_connector_grant_handler(tmp_path, latchkey)
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        host_by_agent={agent: str(host)},
        name_by_agent={agent: "My Machine"},
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        request_event_handlers=(handler,),
    )

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["permissions_unavailable"] is False
    assert len(payload["services_overview"]) == 1
    service = payload["services_overview"][0]
    assert service["service_name"] == "slack"
    assert service["display_name"] == "Slack"
    assert len(service["accounts"]) == 1
    account = service["accounts"][0]
    assert account["account"] == _TEST_ACCOUNT
    assert account["is_connected"] is True
    assert len(account["workspace_grants"]) == 1
    grant = account["workspace_grants"][0]
    assert grant["workspace_agent_id"] == agent
    assert grant["workspace_name"] == "My Machine"
    assert grant["host_id"] == str(host)
    assert [permission["label"] for permission in grant["permissions"]] == ["slack-read-all"]
    assert grant["permissions"][0]["description"] == "All read operations across the Slack API."


def test_settings_overview_flags_an_account_with_no_stored_credentials(tmp_path: Path) -> None:
    """A grant for an account latchkey has no credentials for stays visible, flagged not connected.

    Such a grant is inert (latchkey never injects credentials it does not
    have), but the user must still see it to revoke it.
    """
    agent, host = str(AgentId()), HostId()
    latchkey = _ConnectorLatchkey(latchkey_directory=tmp_path, latchkey_binary="/nonexistent")
    _seed_slack_grant(latchkey, host, "gone@x", ("slack-read-all",))
    handler = _build_connector_grant_handler(tmp_path, latchkey)
    resolver = _WorkspaceResolver(
        url_by_agent_and_service={},
        host_by_agent={agent: str(host)},
        name_by_agent={agent: "My Machine"},
    )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=True,
        backend_resolver=resolver,
        request_event_handlers=(handler,),
    )

    response = client.get("/ui/api/settings")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert len(payload["services_overview"]) == 1
    account = payload["services_overview"][0]["accounts"][0]
    assert account["account"] == "gone@x"
    assert account["is_connected"] is False
    # It is still revocable: the workspace grant carries the revoke keys.
    assert account["workspace_grants"][0]["workspace_name"] == "My Machine"
    assert [permission["label"] for permission in account["workspace_grants"][0]["permissions"]] == ["slack-read-all"]
