import json
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import ClientEnvConfig
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.minds_config import DEFAULT_UPDATE_WINDOW
from imbue.minds.desktop_client.minds_config import MindsConfig
from imbue.minds.desktop_client.minds_config import NotificationStyle
from imbue.minds.desktop_client.testing import WriteCountingMindsConfig
from imbue.minds.desktop_client.ui_api_settings import compute_error_reporting_version
from imbue.minds.desktop_client.ui_api_settings import compute_notification_prefs_version
from imbue.minds.utils.sentry.core import latchkey_forward_sentry_consent_path


def test_settings_overview_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/settings")

    assert response.status_code == 401


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


def test_update_window_write_round_trips_onto_the_overview(tmp_path: Path) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )
    overview = json.loads(client.get("/ui/api/settings").data)
    assert (overview["update_window_start_hour"], overview["update_window_end_hour"]) == (DEFAULT_UPDATE_WINDOW)

    response = client.post("/ui/api/settings/update-window", json={"start_hour": 23, "end_hour": 3})

    assert response.status_code == 200
    assert minds_config.get_update_window() == (23, 3)
    reread = json.loads(client.get("/ui/api/settings").data)
    assert (reread["update_window_start_hour"], reread["update_window_end_hour"]) == (23, 3)


@pytest.mark.parametrize(
    "body",
    (
        {"start_hour": 2, "end_hour": 24},
        {"start_hour": -1, "end_hour": 5},
        {"start_hour": 3, "end_hour": 3},
        {"start_hour": 2},
        {"start_hour": 2, "end_hour": "midnight"},
    ),
    ids=["hour-too-high", "hour-negative", "empty-window", "half-a-window", "not-a-number"],
)
def test_an_unusable_update_window_is_rejected_without_being_stored(tmp_path: Path, body: dict[str, object]) -> None:
    minds_config = MindsConfig(data_dir=tmp_path / "config")
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path, is_authenticated=True, minds_config=minds_config
    )

    response = client.post("/ui/api/settings/update-window", json=body)

    assert response.status_code == 400
    assert minds_config.get_update_window() == DEFAULT_UPDATE_WINDOW


def test_update_window_write_requires_authentication(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=False)

    assert client.post("/ui/api/settings/update-window", json={"start_hour": 1, "end_hour": 4}).status_code == 401


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
    assert minds_config.get_notification_prefs() == (False, "cards", True)
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
    assert minds_config.get_notification_prefs() == (False, "os", True)


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
    assert payload["workspace_id"] == ""
    assert "opened from a machine" in payload["error_message"]


def test_ai_keys_context_reports_a_missing_account_association(tmp_path: Path) -> None:
    client, _app, _auth_store = build_desktop_client_for_test(tmp_path, is_authenticated=True)

    response = client.get("/ui/api/ai-keys?workspace=host-00000000000000000000000000000abc")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["workspace_id"] == "host-00000000000000000000000000000abc"
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
