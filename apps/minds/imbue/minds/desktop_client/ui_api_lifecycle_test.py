import json
from collections.abc import Iterator
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

import pytest
from flask.testing import FlaskClient
from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backend_resolver import AgentDisplayInfo
from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.backup_reaper import BackupReaperManager
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import build_desktop_client_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.environment_signals import ConnectivityDetector
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.testing import build_resolver_with_provider_backend
from imbue.minds.desktop_client.testing import build_resolver_with_system_services
from imbue.minds.desktop_client.testing import build_stub_connectivity_detector
from imbue.minds.desktop_client.testing import record_provider_discovery_error
from imbue.minds.desktop_client.ui_api_lifecycle import _build_ssh_command
from imbue.minds.desktop_client.ui_api_lifecycle import _resolve_workspace_coordinate_to_agent_id
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.primitives import AgentId
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason
from imbue.mngr_forward.ssh_tunnel import RemoteSSHInfo

_DESTROYED_AGENT_ID = "agent-" + "9" * 32
_TEST_USER_ID = "user-test-123"
_TEST_EMAIL = "test@example.com"


def _seed_destroyed_record(tmp_path: Path, cli: FakeImbueCloudCli, destroyed_days_ago: int = 3) -> None:
    seed_store = make_session_store_for_test(tmp_path, cli=cli)
    assert seed_store.record_store is not None
    destroyed_at = (datetime.now(timezone.utc) - timedelta(days=destroyed_days_ago)).isoformat()
    seed_store.record_store.upsert_local_record(
        _TEST_USER_ID,
        _TEST_EMAIL,
        ReplicaRecord(
            host_id="host-destroyed1",
            agent_id=_DESTROYED_AGENT_ID,
            display_name="old-workspace",
            state="destroyed",
            destroyed_at=destroyed_at,
        ),
    )


def _build_lifecycle_client(
    tmp_path: Path,
    cli: FakeImbueCloudCli | None = None,
    is_authenticated: bool = True,
    is_reaper_wired: bool = False,
    backend_resolver: BackendResolverInterface | None = None,
    tracker: SystemInterfaceHealthTracker | None = None,
    connectivity_detector: ConnectivityDetector | None = None,
) -> tuple[FlaskClient, MultiAccountSessionStore]:
    """A desktop-client test app with the stores the lifecycle routes read.

    ``backend_resolver`` and ``tracker`` are what the recovery-info route reads
    its verdict off; the defaults are the empty resolver and no tracker, which is
    what the routes that only resolve coordinates need.
    """
    effective_cli = cli if cli is not None else make_fake_imbue_cloud_cli()
    session_store = make_session_store_for_test(tmp_path, cli=effective_cli)
    sync_scheduler = None
    if is_reaper_wired:
        record_store = session_store.record_store
        assert record_store is not None
        reaper = BackupReaperManager(
            paths=record_store.paths,
            record_store=record_store,
            imbue_cloud_cli=None,
            connector_url="",
        )
        sync_scheduler = WorkspaceSyncScheduler(
            record_store=record_store,
            session_store=session_store,
            resolver=StaticBackendResolver(url_by_agent_and_service={}),
            backup_reaper=reaper,
        )
    client, _app, _auth_store = build_desktop_client_for_test(
        tmp_path,
        is_authenticated=is_authenticated,
        backend_resolver=(
            backend_resolver if backend_resolver is not None else StaticBackendResolver(url_by_agent_and_service={})
        ),
        paths=InstallationPaths(data_dir=tmp_path),
        session_store=session_store,
        sync_scheduler=sync_scheduler,
        system_interface_health_tracker=tracker,
        connectivity_detector=connectivity_detector,
    )
    return client, session_store


def test_destroyed_workspaces_endpoint_requires_a_session(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path, is_authenticated=False)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 401
    assert json.loads(response.data)["error"] == "Not authenticated"


def test_destroyed_workspaces_endpoint_lists_tombstoned_records_as_typed_rows(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["retention_days"] >= 1
    assert len(payload["rows"]) == 1
    row = payload["rows"][0]
    assert row["agent_id"] == _DESTROYED_AGENT_ID
    assert row["display_name"] == "old-workspace"
    assert row["account_label"] == _TEST_EMAIL
    assert "until deletion" in row["days_left_display"]
    assert row["can_delete"] is True
    # No local env and no synced secrets: nothing to download, nothing locked.
    assert row["has_backup"] is False
    assert row["can_download"] is False
    assert row["is_locked"] is False


def test_destroyed_workspaces_endpoint_returns_empty_rows_when_nothing_is_destroyed(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/destroyed-workspaces")

    assert response.status_code == 200
    assert json.loads(response.data)["rows"] == []


def test_delete_destroyed_backup_reaps_the_record(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, session_store = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post(f"/ui/api/destroyed-workspaces/{_DESTROYED_AGENT_ID}/delete-backup")

    assert response.status_code == 200
    assert json.loads(response.data) == {"is_deleted": True}
    assert session_store.record_store is not None
    assert session_store.record_store.list_records(_TEST_USER_ID) == []


def test_delete_destroyed_backup_rejects_an_unknown_agent(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post("/ui/api/destroyed-workspaces/agent-doesnotexist/delete-backup")

    assert response.status_code == 404
    assert "No destroyed machine found" in json.loads(response.data)["error"]


def test_delete_destroyed_backup_rejects_a_well_formed_but_unknown_agent(tmp_path: Path) -> None:
    """A parseable AgentId with no tombstoned record and no orphan env must 404
    rather than reporting a successful deletion of nothing."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=True)

    response = client.post(f"/ui/api/destroyed-workspaces/{AgentId()}/delete-backup")

    assert response.status_code == 404
    assert "No destroyed machine found" in json.loads(response.data)["error"]


def test_delete_destroyed_backup_without_a_reaper_is_a_conflict(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli, is_reaper_wired=False)

    response = client.post(f"/ui/api/destroyed-workspaces/{_DESTROYED_AGENT_ID}/delete-backup")

    assert response.status_code == 409
    assert "not configured" in json.loads(response.data)["error"]


def test_recovery_info_requires_a_session(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path, is_authenticated=False)

    response = client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info")

    assert response.status_code == 401


def test_recovery_info_resolves_an_agent_keyed_workspace_with_healthy_defaults(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path)

    response = client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info")

    assert response.status_code == 200
    payload = json.loads(response.data)
    assert payload["agent_id"] == _DESTROYED_AGENT_ID
    # No tracker wired in this minimal app: health defaults to healthy and
    # the name falls back to the agent id.
    assert payload["health"] == "healthy"
    assert payload["workspace_name"] == _DESTROYED_AGENT_ID
    assert payload["ssh_command"] == ""
    assert payload["is_host_offline"] is False


def test_recovery_info_resolves_a_host_keyed_coordinate_through_workspace_records(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_TEST_USER_ID, email=_TEST_EMAIL)
    _seed_destroyed_record(tmp_path, cli)
    client, _ = _build_lifecycle_client(tmp_path, cli=cli)

    response = client.get("/ui/api/workspaces/host-destroyed1/recovery-info")

    assert response.status_code == 200
    assert json.loads(response.data)["agent_id"] == _DESTROYED_AGENT_ID


def test_recovery_info_rejects_an_unknown_coordinate(tmp_path: Path) -> None:
    client, _ = _build_lifecycle_client(tmp_path)

    response = client.get("/ui/api/workspaces/host-00000000000000000000000000000abc/recovery-info")

    assert response.status_code == 404


def test_recovery_info_carries_the_live_tracker_state_for_the_card(tmp_path: Path) -> None:
    """The card renders its verdict and failure reason from this route's payload."""
    tracker = SystemInterfaceHealthTracker()
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    client, _ = _build_lifecycle_client(tmp_path, tracker=tracker)

    tracker.mark_restarting(agent_id, start_only=False)
    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)
    assert payload["health"] == "restarting"

    tracker.mark_restart_failed(agent_id, "Start step of host restart failed: exited 1")
    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)
    assert payload["health"] == "restart_failed"
    assert payload["health_error"] == "Start step of host restart failed: exited 1"


def test_recovery_info_picks_up_a_provider_error_that_lands_after_the_card_opened(tmp_path: Path) -> None:
    """A backend outage that arrives mid-episode changes what the route reports.

    This is the whole point of the card polling: the first read describes an
    unresponsive machine, and when discovery then attributes the outage to the
    provider, the route says so -- so the card can stop offering a restart that
    would route through the very backend that is down.
    """
    resolver = build_resolver_with_system_services(AgentId(_DESTROYED_AGENT_ID), AgentId.generate())
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)
    assert payload["is_backend_unreachable"] is False
    assert payload["unreachable_reason"] == ""

    record_provider_discovery_error(
        resolver, "docker", "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"
    )

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)
    assert payload["is_backend_unreachable"] is True
    assert payload["provider_label"] == "Docker"
    assert payload["unreachable_reason"] == "Cannot connect to the Docker daemon at unix:///var/run/docker.sock"


def test_recovery_info_reports_the_backend_a_restart_was_already_rejected_at(tmp_path: Path) -> None:
    """The card's very first read carries the verdict, because the restart already has it.

    The card is raised by the restart-failed edge, so this route is called
    moments after the restart that failed -- and long before the provider's next
    poll. Discovery has surfaced nothing here, exactly as it had not when the
    card opened; the reason comes from the restart mngr rejected at the backend.
    """
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    resolver = build_resolver_with_system_services(agent_id, AgentId.generate())
    tracker = SystemInterfaceHealthTracker()
    tracker.record_backend_outage(agent_id, "docker", "Docker Desktop is manually paused.")
    tracker.mark_restart_failed(
        agent_id,
        "This machine's backend is unreachable, so the restart could not run: Docker Desktop is manually paused.",
    )
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver, tracker=tracker)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)

    assert payload["health"] == "restart_failed"
    assert payload["is_backend_unreachable"] is True
    assert payload["provider_label"] == "Docker"
    assert payload["unreachable_reason"] == "Docker Desktop is manually paused."


def test_recovery_info_reports_a_dead_network_for_a_machine_it_can_explain(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """The card falls back to this, so a machine on the far side of a dead network says so."""
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    resolver = build_resolver_with_provider_backend(
        agent_id, provider_name="imbue_cloud_someone", backend="imbue_cloud"
    )
    detector, _ = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)
    detector.probe_now()
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver, connectivity_detector=detector)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)

    assert payload["device_environment"] == "OFFLINE"


@pytest.mark.parametrize("backend", ["local", "docker", "lima"])
def test_recovery_info_withholds_the_device_condition_from_a_machine_on_this_device(
    tmp_path: Path,
    backend: str,
    root_concurrency_group: ConcurrencyGroup,
) -> None:
    """A dead network explains nothing about a container on this laptop, and must not disarm its card.

    The card renders this field with no locality of its own: a non-NONE value
    replaces the Restart button with a waiting-for-network line. On an on-device
    machine that restart is exactly what would fix it -- it runs over loopback,
    which the wifi has no say in -- so reporting the device's condition here
    would take away a working affordance over a condition that does not apply.
    """
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    resolver = build_resolver_with_provider_backend(agent_id, provider_name=backend, backend=backend)
    detector, _ = build_stub_connectivity_detector(root_concurrency_group, is_internet_up=False)
    detector.probe_now()
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver, connectivity_detector=detector)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)

    assert payload["device_environment"] == "NONE"


def test_recovery_info_reports_a_connection_this_device_could_not_make(tmp_path: Path) -> None:
    """The card's verdict when the failure never left this device, with the error that proves it.

    The machine is very likely running fine, so the card must stop offering to
    restart it -- and the verbatim error is the only thing that makes a broken
    local install (a missing known_hosts, say) diagnosable from inside the app.
    """
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    resolver = build_resolver_with_system_services(agent_id, AgentId.generate())
    tracker = SystemInterfaceHealthTracker()
    tracker.record_connection_failure(
        agent_id,
        SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED,
        "No known_hosts file at /keys/known_hosts; refusing to connect without a pinned host key",
    )
    tracker.mark_stuck(agent_id)
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver, tracker=tracker)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)

    assert payload["is_device_cannot_connect"] is True
    assert "No known_hosts file" in payload["device_error_detail"]
    # The machine's backend is a separate question, and nothing here answers it.
    assert payload["is_backend_unreachable"] is False


def test_recovery_info_stops_blaming_this_device_once_the_machine_answers(tmp_path: Path) -> None:
    """A probe that reaches the machine ends the episode, and the verdict with it."""
    agent_id = AgentId(_DESTROYED_AGENT_ID)
    resolver = build_resolver_with_system_services(agent_id, AgentId.generate())
    tracker = SystemInterfaceHealthTracker()
    tracker.record_connection_failure(agent_id, SystemInterfaceBackendFailureReason.POOL_EXHAUSTED, "pool timeout")
    client, _ = _build_lifecycle_client(tmp_path, backend_resolver=resolver, tracker=tracker)
    assert json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)[
        "is_device_cannot_connect"
    ]

    tracker.record_probe_success(agent_id)

    payload = json.loads(client.get(f"/ui/api/workspaces/{_DESTROYED_AGENT_ID}/recovery-info").data)
    assert payload["is_device_cannot_connect"] is False
    assert payload["device_error_detail"] == ""


def test_build_ssh_command_renders_the_resolvers_ssh_info() -> None:
    """The recovery page's SSH command matches what mngr emits for the host."""
    agent_id = AgentId()
    resolver = StaticBackendResolver(
        url_by_agent_and_service={},
        ssh_info_by_agent_id={
            str(agent_id): RemoteSSHInfo(user="root", host="127.0.0.1", port=60022, key_path=Path("/home/u/.mngr/key"))
        },
    )
    assert _build_ssh_command(resolver, agent_id) == "ssh -i /home/u/.mngr/key -p 60022 root@127.0.0.1"


def test_build_ssh_command_is_empty_without_ssh_info() -> None:
    """An agent the resolver has no SSH info for yields no command (the copy button is then omitted)."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})
    assert _build_ssh_command(resolver, AgentId()) == ""


# -- _resolve_workspace_coordinate_to_agent_id ------------------------------
#
# Workspace content URLs are keyed by host id while minds' records stay
# agent-keyed; the resolver translates between the two coordinates for the
# recovery page.

_AGENT_A = AgentId("agent-" + "a" * 32)
_AGENT_B = AgentId("agent-" + "b" * 32)
_HOST_A = "host-" + "a" * 32
_HOST_B = "host-" + "b" * 32


class _HostAwareResolver(StaticBackendResolver):
    """StaticBackendResolver that also carries the agent -> host coordinate map."""

    host_id_by_agent_id: Mapping[str, str] = Field(
        default_factory=dict, frozen=True, description="Agent id -> host id, mirroring discovery"
    )

    def get_agent_display_info(self, agent_id: AgentId) -> AgentDisplayInfo | None:
        host_id = self.host_id_by_agent_id.get(str(agent_id))
        if host_id is None:
            return None
        return AgentDisplayInfo(agent_name=str(agent_id), host_id=host_id)


def _resolver(host_id_by_agent_id: Mapping[str, str]) -> _HostAwareResolver:
    return _HostAwareResolver(
        url_by_agent_and_service={aid: {} for aid in host_id_by_agent_id},
        host_id_by_agent_id=host_id_by_agent_id,
    )


def _record(host_id: str, agent_id: str) -> ReplicaRecord:
    return ReplicaRecord(host_id=host_id, agent_id=agent_id)


def test_agent_coordinate_passes_through_without_lookups() -> None:
    resolved = _resolve_workspace_coordinate_to_agent_id(str(_AGENT_A), _resolver({}), [])
    assert resolved == _AGENT_A


def test_malformed_agent_coordinate_resolves_to_none() -> None:
    assert _resolve_workspace_coordinate_to_agent_id("agent-nothex", _resolver({}), []) is None


@pytest.mark.parametrize("workspace_id", ["", "workspace-1", "localhost", "create"])
def test_non_coordinate_strings_resolve_to_none(workspace_id: str) -> None:
    assert _resolve_workspace_coordinate_to_agent_id(workspace_id, _resolver({}), []) is None


def test_host_coordinate_resolves_via_discovery() -> None:
    resolver = _resolver({str(_AGENT_A): _HOST_A, str(_AGENT_B): _HOST_B})
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_B, resolver, []) == _AGENT_B


def test_host_coordinate_resolution_does_not_touch_records_on_a_discovery_hit() -> None:
    """Records are the fallback: a discovery hit must not list them (they can be slow)."""

    def _exploding_records() -> Iterator[ReplicaRecord]:
        raise AssertionError("records must not be listed when discovery resolves the host id")
        # The unreachable yield makes this a generator, so the raise only
        # fires if the records are actually iterated.
        yield

    resolver = _resolver({str(_AGENT_A): _HOST_A})
    resolved = _resolve_workspace_coordinate_to_agent_id(_HOST_A, resolver, _exploding_records())
    assert resolved == _AGENT_A


def test_host_coordinate_falls_back_to_workspace_records() -> None:
    """A stopped host discovery no longer reports still resolves via the record replica."""
    records = [_record(_HOST_A, str(_AGENT_A)), _record(_HOST_B, str(_AGENT_B))]
    resolved = _resolve_workspace_coordinate_to_agent_id(_HOST_B, _resolver({}), records)
    assert resolved == _AGENT_B


def test_host_coordinate_record_fallback_skips_agentless_records() -> None:
    records = [_record(_HOST_A, "")]
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_A, _resolver({}), records) is None


def test_unknown_host_coordinate_resolves_to_none() -> None:
    resolver = _resolver({str(_AGENT_A): _HOST_A})
    assert _resolve_workspace_coordinate_to_agent_id(_HOST_B, resolver, []) is None
