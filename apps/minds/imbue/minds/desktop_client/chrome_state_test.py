"""Byte-identity tests for the chrome-state wire format.

The expected dicts below are copied verbatim from the hand-built payloads the
SSE route emitted before ``chrome_state.py`` existed. Each test asserts the
model serialization produces the exact same ``json.dumps`` bytes -- key
order, present/absent optional keys, and null-vs-absent semantics included --
so adopting the models could not change what ES5 consumers see.
"""

import json

from imbue.minds.desktop_client.chrome_state import ChromeBootState
from imbue.minds.desktop_client.chrome_state import ChromeProviderEntry
from imbue.minds.desktop_client.chrome_state import ChromeProviderStatus
from imbue.minds.desktop_client.chrome_state import ChromeProvidersPayload
from imbue.minds.desktop_client.chrome_state import ChromeRequestsPayload
from imbue.minds.desktop_client.chrome_state import ChromeSystemInterfaceStatusPayload
from imbue.minds.desktop_client.chrome_state import ChromeWorkspaceEntry
from imbue.minds.desktop_client.chrome_state import ChromeWorkspacesPayload


def _dumps(payload: object) -> str:
    return json.dumps(payload)


def test_workspace_entry_minimal_matches_legacy_dict_bytes() -> None:
    entry = ChromeWorkspaceEntry(id="agent-1", name="ws", accent="#0b292b")

    assert _dumps(entry.to_payload_dict()) == _dumps({"id": "agent-1", "name": "ws", "accent": "#0b292b"})


def test_workspace_entry_full_local_matches_legacy_key_order() -> None:
    """A stale, shutdown-capable, account-owned local row: optional keys appear
    in the exact order the legacy imperative construction inserted them."""
    entry = ChromeWorkspaceEntry(
        id="agent-1",
        name="ws",
        accent="#0b292b",
        is_stale="true",
        supports_shutdown="true",
        liveness="RUNNING",
        account="alice@example.com",
    )

    assert _dumps(entry.to_payload_dict()) == _dumps(
        {
            "id": "agent-1",
            "name": "ws",
            "accent": "#0b292b",
            "is_stale": "true",
            "supports_shutdown": "true",
            "liveness": "RUNNING",
            "account": "alice@example.com",
        }
    )


def test_workspace_entry_remote_matches_legacy_key_order() -> None:
    entry = ChromeWorkspaceEntry(
        id="agent-2",
        name="remote-ws",
        accent="#123456",
        is_remote="true",
        location="my-laptop",
        account="alice@example.com",
    )

    assert _dumps(entry.to_payload_dict()) == _dumps(
        {
            "id": "agent-2",
            "name": "remote-ws",
            "accent": "#123456",
            "is_remote": "true",
            "location": "my-laptop",
            "account": "alice@example.com",
        }
    )


def test_workspaces_connect_snapshot_matches_legacy_bytes() -> None:
    """The connect-time event carries has_accounts + restorable_workspace_ids
    between destroying_agent_ids and remote_workspace_states, exactly as the
    legacy dict literal did."""
    payload = ChromeWorkspacesPayload(
        workspaces=(ChromeWorkspaceEntry(id="agent-1", name="ws", accent="#0b292b"),),
        destroying_agent_ids=("agent-9",),
        has_accounts=True,
        restorable_workspace_ids=("agent-1", "agent-9"),
        remote_workspace_states={"agent-2": "connecting"},
    )

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {
            "type": "workspaces",
            "workspaces": [{"id": "agent-1", "name": "ws", "accent": "#0b292b"}],
            "destroying_agent_ids": ["agent-9"],
            "has_accounts": True,
            "restorable_workspace_ids": ["agent-1", "agent-9"],
            "remote_workspace_states": {"agent-2": "connecting"},
        }
    )


def test_workspaces_update_event_omits_connect_only_fields() -> None:
    payload = ChromeWorkspacesPayload(
        workspaces=(),
        destroying_agent_ids=(),
        remote_workspace_states={},
    )

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {
            "type": "workspaces",
            "workspaces": [],
            "destroying_agent_ids": [],
            "remote_workspace_states": {},
        }
    )


def test_providers_payload_matches_legacy_bytes_across_all_statuses() -> None:
    """ok / error / disabled entries: ``backend`` is null-but-present on
    error/disabled rows while the error fields are absent on non-error rows."""
    payload = ChromeProvidersPayload(
        providers=(
            ChromeProviderEntry(name="docker", backend=None, status=ChromeProviderStatus.DISABLED, is_enabled=False),
            ChromeProviderEntry(
                name="modal",
                backend=None,
                status=ChromeProviderStatus.ERROR,
                is_enabled=True,
                error_type="ImbueCloudAuthError",
                error_message="token missing",
            ),
            ChromeProviderEntry(name="zzz", backend="docker", status=ChromeProviderStatus.OK, is_enabled=True),
        ),
        last_event_at="2026-07-20T00:00:00+00:00",
        last_full_snapshot_at=None,
    )

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {
            "type": "providers_state",
            "providers": [
                {"name": "docker", "backend": None, "status": "disabled", "is_enabled": False},
                {
                    "name": "modal",
                    "backend": None,
                    "status": "error",
                    "is_enabled": True,
                    "error_type": "ImbueCloudAuthError",
                    "error_message": "token missing",
                },
                {"name": "zzz", "backend": "docker", "status": "ok", "is_enabled": True},
            ],
            "last_event_at": "2026-07-20T00:00:00+00:00",
            "last_full_snapshot_at": None,
        }
    )


def test_providers_payload_empty_default_matches_legacy_bytes() -> None:
    payload = ChromeProvidersPayload(providers=(), last_event_at=None, last_full_snapshot_at=None)

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {"type": "providers_state", "providers": [], "last_event_at": None, "last_full_snapshot_at": None}
    )


def test_requests_payload_matches_legacy_bytes() -> None:
    payload = ChromeRequestsPayload(count=2, request_ids=("evt-1", "evt-2"), auto_open=False)

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {"type": "requests", "count": 2, "request_ids": ["evt-1", "evt-2"], "auto_open": False}
    )


def test_system_interface_status_matches_legacy_bytes_without_error() -> None:
    payload = ChromeSystemInterfaceStatusPayload(agent_id="agent-1", status="stuck")

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {"type": "system_interface_status", "agent_id": "agent-1", "status": "stuck"}
    )


def test_system_interface_status_matches_legacy_bytes_with_error() -> None:
    payload = ChromeSystemInterfaceStatusPayload(agent_id="agent-1", status="restart_failed", error="boom")

    assert _dumps(payload.to_payload_dict()) == _dumps(
        {"type": "system_interface_status", "agent_id": "agent-1", "status": "restart_failed", "error": "boom"}
    )


def test_boot_state_bundles_the_event_payloads_by_name() -> None:
    boot_state = ChromeBootState(
        workspaces=ChromeWorkspacesPayload(
            workspaces=(),
            destroying_agent_ids=(),
            has_accounts=False,
            restorable_workspace_ids=(),
            remote_workspace_states={},
        ),
        providers=ChromeProvidersPayload(providers=(), last_event_at=None, last_full_snapshot_at=None),
        requests=ChromeRequestsPayload(count=0, request_ids=(), auto_open=True),
        system_interface_statuses=(ChromeSystemInterfaceStatusPayload(agent_id="agent-1", status="healthy"),),
    )

    payload = boot_state.to_payload_dict()

    assert list(payload.keys()) == ["workspaces", "providers", "requests", "system_interface_statuses"]
    assert payload["workspaces"]["type"] == "workspaces"
    assert payload["providers"]["type"] == "providers_state"
    assert payload["requests"]["type"] == "requests"
    assert payload["system_interface_statuses"] == [
        {"type": "system_interface_status", "agent_id": "agent-1", "status": "healthy"}
    ]
    # The island must round-trip through JSON (what the Jinja ``tojson`` emits).
    assert json.loads(json.dumps(payload)) == payload
