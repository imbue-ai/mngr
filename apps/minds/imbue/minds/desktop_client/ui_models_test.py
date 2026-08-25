import json

import pytest
from inline_snapshot import snapshot
from pydantic import ValidationError

from imbue.minds.desktop_client.system_interface_health import AgentHealth
from imbue.minds.desktop_client.ui_models import UI_CLIENT_MESSAGE_ADAPTER
from imbue.minds.desktop_client.ui_models import UI_SCHEMA_VERSION
from imbue.minds.desktop_client.ui_models import UiClientStateMessage
from imbue.minds.desktop_client.ui_models import UiHealthMessage
from imbue.minds.desktop_client.ui_models import UiHelloMessage
from imbue.minds.desktop_client.ui_models import UiWireSchema
from imbue.minds.desktop_client.ui_models import UiWorkspaceEntry
from imbue.minds.desktop_client.ui_models import UiWorkspacesMessage


def test_schema_version_tracks_breaking_wire_changes() -> None:
    """Bumped to 2 when the inbox detail payload replaced its flat permission
    lists with server-grouped rows, to 3 when every offered connection started
    carrying how it is connected, and to 4 when the requests frame and the
    inbox list response dropped ``auto_open`` (and the predefined detail gained
    ``service_name``), to 5 when workspace entries replaced ``is_stale``
    with ``is_backend_unreachable``, and to 6 when the snapshot gained a
    required ``environment`` frame carrying this device's own connectivity
    condition: a window held open across any of these upgrades would otherwise
    reconnect and act on a payload it does not know -- for 3, by offering a
    browser sign-in to a service that has none; for 4, by expecting a field the
    server no longer sends; for 5, by reading a field that is gone and so never
    naming the backend its band is about; for 6, by reading the state off a
    snapshot field the older server does not send at all."""
    assert UI_SCHEMA_VERSION == 6


def test_hello_message_serializes_with_type_discriminator() -> None:
    # The version travels as a literal here, not as the constant the frame was
    # built from: comparing a field to the value just passed into it cannot
    # fail, whatever the constant becomes.
    frame = UiHelloMessage(schema_version=UI_SCHEMA_VERSION).model_dump_json()
    parsed = json.loads(frame)
    assert parsed == {"type": "hello", "schema_version": 6}


def test_workspaces_message_round_trips_through_json() -> None:
    message = UiWorkspacesMessage(
        workspaces=(
            UiWorkspaceEntry(
                id="agent-0123456789abcdef0123456789abcdef",
                name="my workspace",
                accent="#aabbcc",
                host_id="host-0123456789abcdef0123456789abcdef",
                supports_shutdown=True,
                liveness="RUNNING",
            ),
        ),
        destroying_agent_ids=("agent-ffffffffffffffffffffffffffffffff",),
        restorable_workspace_ids=("agent-0123456789abcdef0123456789abcdef",),
        remote_workspace_states={"agent-11111111111111111111111111111111": "connecting"},
    )

    restored = UiWorkspacesMessage.model_validate_json(message.model_dump_json())

    assert restored == message
    assert restored.workspaces[0].supports_shutdown is True
    assert restored.workspaces[0].is_remote is False


def test_health_message_carries_enum_status_as_wire_string() -> None:
    frame = UiHealthMessage(agent_id="agent-abc", status=AgentHealth.RESTART_FAILED, error="boom").model_dump_json()
    parsed = json.loads(frame)
    assert parsed["status"] == "restart_failed"
    assert parsed["error"] == "boom"


def test_client_message_adapter_parses_client_state() -> None:
    raw = json.dumps({"type": "client_state", "client_id": "win-1", "route": "/settings", "workspace_agent_id": None})
    parsed = UI_CLIENT_MESSAGE_ADAPTER.validate_json(raw)
    assert isinstance(parsed, UiClientStateMessage)
    assert parsed.route == "/settings"


def test_client_message_adapter_rejects_server_message_types() -> None:
    with pytest.raises(ValidationError):
        UI_CLIENT_MESSAGE_ADAPTER.validate_json(json.dumps({"type": "hello", "schema_version": 1}))


def test_wire_schema_defs_inventory_is_stable() -> None:
    """The generated-TS contract: any def appearing/disappearing must be a conscious change."""
    schema = UiWireSchema.model_json_schema()
    assert sorted(schema["$defs"].keys()) == snapshot(
        [
            "AgentHealth",
            "DiscoveryHealth",
            "EnvironmentBlock",
            "NotificationOutcome",
            "ProviderPanelStatus",
            "UiAccountsMessage",
            "UiAvailableConnection",
            "UiBootstrap",
            "UiBootstrapSeed",
            "UiClientStateMessage",
            "UiConnectCredentialsRequest",
            "UiConnectorDisconnectRequest",
            "UiConnectorRevokeAllRequest",
            "UiConnectorToggleRequest",
            "UiCredentialParameter",
            "UiDiscoveryHealthMessage",
            "UiEnvironmentMessage",
            "UiHealthMessage",
            "UiHelloMessage",
            "UiNotificationEntry",
            "UiNotificationsMessage",
            "UiOpenHelpMessage",
            "UiPermissionConnection",
            "UiPermissionGrantGroup",
            "UiPermissionGrantRow",
            "UiPermissionScopePanel",
            "UiPermissionToggle",
            "UiPermissionToggleGroup",
            "UiProviderEntry",
            "UiProvidersMessage",
            "UiReloadMessage",
            "UiRequestsMessage",
            "UiSelfPermissionToggle",
            "UiSelfToggleRequest",
            "UiServiceSignIn",
            "UiSnapshot",
            "UiWaitingPermissionRequest",
            "UiWorkspaceEntry",
            "UiWorkspacePermissions",
            "UiWorkspaceRefreshMessage",
            "UiWorkspaceStoppedMessage",
            "UiWorkspacesMessage",
        ]
    )


def test_wire_schema_generation_is_deterministic() -> None:
    first = json.dumps(UiWireSchema.model_json_schema(), sort_keys=True)
    second = json.dumps(UiWireSchema.model_json_schema(), sort_keys=True)
    assert first == second
