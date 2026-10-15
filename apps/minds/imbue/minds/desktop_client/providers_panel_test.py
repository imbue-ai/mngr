"""Unit tests for the providers-panel frame builder and the workspace-list builder.

Covers ``_build_providers_state_payload`` (one section per account plus the
local profile, with each connection's status, and the device providers; all
from the discovery snapshot, the session store, and the connection store) and
``_build_workspace_list`` (the backend-unreachable and device-cannot-connect
verdicts + color emission).
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

import pytest

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.app import _build_providers_state_payload
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.backend_resolver import ParsedAgentsResult
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import add_modal_connection_for_test
from imbue.minds.desktop_client.conftest import make_connection_store_for_test
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.conftest import seed_provider_snapshots
from imbue.minds.desktop_client.conftest import unlock_account_with_master_password_for_test
from imbue.minds.desktop_client.connections.connection_store import ConnectionStore
from imbue.minds.desktop_client.connections.primitives import ConnectionBackend
from imbue.minds.desktop_client.connections.primitives import LOCAL_PROFILE_USER_ID
from imbue.minds.desktop_client.connections.primitives import imbue_cloud_provider_name_for_user
from imbue.minds.desktop_client.connections.primitives import provider_instance_name_for_connection
from imbue.minds.desktop_client.dek_store import delete_bundle_mirror
from imbue.minds.desktop_client.dek_store import delete_dek
from imbue.minds.desktop_client.mock_agent_creator_test import InFlightCreateAttempt
from imbue.minds.desktop_client.mock_agent_creator_test import make_in_flight_agent_creator
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.desktop_client.ui_models import AccountPanelState
from imbue.minds.desktop_client.ui_models import ConnectionPanelStatus
from imbue.minds.desktop_client.ui_models import ProviderPanelStatus
from imbue.minds.desktop_client.workspace_color import DEFAULT_WORKSPACE_COLOR
from imbue.minds.desktop_client.workspace_record_store import RECORD_STATE_ACTIVE
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.mngr.api.discovery_events import DiscoveredProvider
from imbue.mngr.api.discovery_events import DiscoveryError
from imbue.mngr.api.discovery_events import make_discovered_provider
from imbue.mngr.config.data_types import ProviderInstanceConfig
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import DiscoveredAgent
from imbue.mngr.primitives import HostId
from imbue.mngr.primitives import ProviderBackendName
from imbue.mngr.primitives import ProviderInstanceName
from imbue.mngr_forward.data_types import SystemInterfaceBackendFailureReason

# _build_providers_state_payload


def _make_discovered_provider(name: str, backend: str = "docker") -> DiscoveredProvider:
    return make_discovered_provider(
        ProviderInstanceName(name),
        ProviderInstanceConfig(backend=ProviderBackendName(backend), is_enabled=True),
    )


def _add_modal_connection(store: ConnectionStore, owner_user_id: str, alias: str) -> str:
    """Add a fake Modal connection and return its instance name (what the panel keys rows on)."""
    return provider_instance_name_for_connection(
        ConnectionBackend.MODAL, add_modal_connection_for_test(store, owner_user_id, alias)
    )


def _unlocked_account_with_a_modal_connection(
    tmp_path: Path, user_id: str, email: str
) -> tuple[MultiAccountSessionStore, InstallationPaths, ConnectionStore, str]:
    """A signed-in account, unlocked with a master password set, owning one Modal connection ("team modal")."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    store = make_connection_store_for_test(tmp_path / "minds", cli)
    session_store = make_session_store_for_test(tmp_path / "minds", cli, connection_store=store)
    paths = InstallationPaths(data_dir=tmp_path / "minds")
    unlock_account_with_master_password_for_test(paths, user_id)
    instance = _add_modal_connection(store, user_id, "team modal")
    return session_store, paths, store, instance


def test_build_providers_state_payload_has_only_the_local_section_for_a_non_mngr_resolver() -> None:
    """A non-``MngrCliBackendResolver`` resolver contributes no discovery: the local profile section is all there is."""
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    payload = _build_providers_state_payload(resolver, None, None, None)

    assert [section.user_id for section in payload.accounts] == [LOCAL_PROFILE_USER_ID]
    assert payload.accounts[0].connections == () and payload.accounts[0].imbue_cloud is None
    assert payload.device_providers == ()
    assert payload.last_event_at is None and payload.last_full_snapshot_at is None


@pytest.mark.parametrize("name", ["local", "imbue_cloud", "aws", "gcp", "azure", "modal"])
def test_build_providers_state_payload_hides_local_and_the_suppressed_default_instances(name: str) -> None:
    resolver = MngrCliBackendResolver()
    now = datetime.now(timezone.utc)
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider(name, backend=name),),
        error_by_provider_name={},
        last_snapshot_at=now,
    )

    payload = _build_providers_state_payload(resolver, None, None, None)

    assert payload.device_providers == ()
    assert payload.last_full_snapshot_at == now.isoformat()
    assert payload.last_event_at == now.isoformat()


def test_build_providers_state_payload_lists_device_providers_sorted_with_their_verdicts() -> None:
    """docker, lima, and friends belong to no account: they form the device section, error verdicts included."""
    errored = ProviderInstanceName("vultr")
    resolver = MngrCliBackendResolver()
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider("lima", backend="lima"), _make_discovered_provider("docker")),
        error_by_provider_name={
            errored: DiscoveryError(type_name="RuntimeError", message="api down", provider_name=errored),
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver, None, None, None)

    assert [entry.name for entry in payload.device_providers] == ["docker", "lima", "vultr"]
    by_name = {entry.name: entry for entry in payload.device_providers}
    assert by_name["docker"].status is ProviderPanelStatus.OK and by_name["docker"].backend == "docker"
    assert by_name["vultr"].status is ProviderPanelStatus.ERROR
    assert by_name["vultr"].error_type == "RuntimeError" and by_name["vultr"].error_message == "api down"


def test_build_providers_state_payload_puts_the_account_instance_under_its_account(tmp_path: Path) -> None:
    """Each signed-in account's section carries its imbue_cloud instance with discovery's verdict on it."""
    healthy_user_id = str(uuid4())
    errored_user_id = str(uuid4())
    unlisted_user_id = str(uuid4())
    cli = make_fake_imbue_cloud_cli()
    for user_id in (healthy_user_id, errored_user_id, unlisted_user_id):
        cli.add_account(user_id=user_id, email=f"{user_id[:8]}@example.com")
    session_store = make_session_store_for_test(tmp_path / "minds", cli)
    errored_instance = ProviderInstanceName(imbue_cloud_provider_name_for_user(errored_user_id))
    resolver = MngrCliBackendResolver()
    seed_provider_snapshots(
        resolver,
        providers=(
            _make_discovered_provider(imbue_cloud_provider_name_for_user(healthy_user_id), backend="imbue_cloud"),
            _make_discovered_provider("imbue_cloud_alice-example-com", backend="imbue_cloud"),
        ),
        error_by_provider_name={
            errored_instance: DiscoveryError(
                type_name="ImbueCloudAuthError", message="token missing", provider_name=errored_instance
            )
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver, session_store, None, None)

    by_user_id = {section.user_id: section for section in payload.accounts}
    assert list(by_user_id) == [healthy_user_id, errored_user_id, unlisted_user_id, LOCAL_PROFILE_USER_ID]
    assert by_user_id[healthy_user_id].state is AccountPanelState.SIGNED_IN
    assert by_user_id[healthy_user_id].email == f"{healthy_user_id[:8]}@example.com"
    healthy = by_user_id[healthy_user_id].imbue_cloud
    assert healthy is not None and healthy.status is ProviderPanelStatus.OK and healthy.backend == "imbue_cloud"
    errored = by_user_id[errored_user_id].imbue_cloud
    assert errored is not None and errored.status is ProviderPanelStatus.ERROR
    assert errored.error_type == "ImbueCloudAuthError" and errored.error_message == "token missing"
    unlisted = by_user_id[unlisted_user_id].imbue_cloud
    assert unlisted is not None and unlisted.status is ProviderPanelStatus.UNLISTED
    # A legacy email-slug account instance is neither a section nor a device provider.
    assert payload.device_providers == ()


def test_build_providers_state_payload_reports_each_connections_status_and_workspace_count(tmp_path: Path) -> None:
    store = make_connection_store_for_test(tmp_path)
    listed_instance = _add_modal_connection(store, LOCAL_PROFILE_USER_ID, "listed modal")
    errored_instance = _add_modal_connection(store, LOCAL_PROFILE_USER_ID, "broken modal")
    unlisted_instance = _add_modal_connection(store, LOCAL_PROFILE_USER_ID, "fresh modal")
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent(listed_instance)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))
    seed_provider_snapshots(
        resolver,
        providers=(_make_discovered_provider(listed_instance, backend="modal"), _make_discovered_provider("docker")),
        error_by_provider_name={
            ProviderInstanceName(errored_instance): DiscoveryError(
                type_name="ProviderNotAuthorizedError",
                message="bad token",
                provider_name=ProviderInstanceName(errored_instance),
            )
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )

    payload = _build_providers_state_payload(resolver, None, store, None)

    (local_section,) = payload.accounts
    by_instance = {entry.instance: entry for entry in local_section.connections}
    # Alias order, not insertion order.
    assert [entry.alias for entry in local_section.connections] == ["broken modal", "fresh modal", "listed modal"]
    assert by_instance[listed_instance].status is ConnectionPanelStatus.OK
    assert by_instance[listed_instance].workspace_count == 1
    assert by_instance[listed_instance].backend == "modal" and by_instance[listed_instance].identifier == "ak…"
    assert by_instance[listed_instance].backend_label == "Modal"
    assert by_instance[errored_instance].status is ConnectionPanelStatus.ERROR
    assert by_instance[errored_instance].error_message == "bad token"
    assert by_instance[unlisted_instance].status is ConnectionPanelStatus.UNLISTED
    assert by_instance[unlisted_instance].workspace_count == 0
    # Connection instances never leak into the device section.
    assert [entry.name for entry in payload.device_providers] == ["docker"]


def test_build_providers_state_payload_counts_a_workspace_in_both_discovery_and_the_records_once(
    tmp_path: Path,
) -> None:
    user_id = str(uuid4())
    email = "owner@example.com"
    session_store, paths, store, instance = _unlocked_account_with_a_modal_connection(tmp_path, user_id, email)
    resolver = MngrCliBackendResolver()
    hosted_here = _make_workspace_agent(instance)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(hosted_here.agent_id,), discovered_agents=(hosted_here,)))
    seed_provider_snapshots(resolver, providers=(_make_discovered_provider(instance, backend="modal"),))
    record_store = session_store.record_store
    assert record_store is not None
    for agent_id in (str(hosted_here.agent_id), str(AgentId.generate())):
        record_store.upsert_local_record(
            user_id,
            email,
            ReplicaRecord(
                host_id=str(HostId.generate()),
                agent_id=agent_id,
                display_name="on the connection",
                provider_kind=instance,
                state=RECORD_STATE_ACTIVE,
            ),
        )

    payload = _build_providers_state_payload(resolver, session_store, store, None)

    (account_section, _local_section) = payload.accounts
    (connection,) = account_section.connections
    assert connection.workspace_count == 2
    assert connection.status is ConnectionPanelStatus.OK


def test_build_providers_state_payload_counts_a_workspace_still_being_created_on_a_connection(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    """A create has no record and no discovered host until `mngr create` returns, so the count reads the creates
    in flight too; one whose host discovery already lists is not counted twice."""
    store = make_connection_store_for_test(tmp_path)
    instance = _add_modal_connection(store, LOCAL_PROFILE_USER_ID, "busy modal")
    resolver = MngrCliBackendResolver()
    discovered = _make_workspace_agent(instance)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(discovered.agent_id,), discovered_agents=(discovered,)))
    seed_provider_snapshots(resolver, providers=(_make_discovered_provider(instance, backend="modal"),))
    agent_creator = make_in_flight_agent_creator(
        tmp_path,
        root_concurrency_group,
        (
            InFlightCreateAttempt(provider_instance_name=instance, host_name="still-creating"),
            InFlightCreateAttempt(
                provider_instance_name=instance, host_name="my-workspace", agent_id=discovered.agent_id
            ),
            InFlightCreateAttempt(provider_instance_name="docker", host_name="elsewhere"),
        ),
    )

    payload = _build_providers_state_payload(resolver, None, store, agent_creator)

    (local_section,) = payload.accounts
    (connection,) = local_section.connections
    assert connection.workspace_count == 2


@pytest.mark.parametrize("is_bundle_mirror_present", [True, False])
def test_build_providers_state_payload_marks_a_locked_account_and_its_connections(
    tmp_path: Path, is_bundle_mirror_present: bool
) -> None:
    """A master password set elsewhere but no DEK here: the section is locked and so is each connection.

    Without the key-bundle mirror too (a fresh device that pulled nothing but the
    connection), the connection's encrypted credentials alone mark the account locked.
    """
    user_id = str(uuid4())
    session_store, paths, store, _ = _unlocked_account_with_a_modal_connection(tmp_path, user_id, "locked@example.com")
    delete_dek(paths, user_id)
    if not is_bundle_mirror_present:
        delete_bundle_mirror(paths, user_id)

    payload = _build_providers_state_payload(MngrCliBackendResolver(), session_store, store, None)

    (account_section, _local_section) = payload.accounts
    assert account_section.state is AccountPanelState.LOCKED
    assert account_section.is_connection_owner_eligible is False
    (connection,) = account_section.connections
    assert connection.status is ConnectionPanelStatus.LOCKED


def test_build_providers_state_payload_lists_a_signed_out_account_that_still_hosts_local_rows(
    tmp_path: Path,
) -> None:
    """An account with no session but ACTIVE rows this device hosts still gets a (signed-out) section, labeled
    with the email its replica was last synced under."""
    user_id = str(uuid4())
    email = "gone@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    session_store = make_session_store_for_test(tmp_path / "minds", cli)
    record_store = session_store.record_store
    assert record_store is not None
    record_store.upsert_local_record(
        user_id,
        email,
        ReplicaRecord(
            host_id=str(HostId.generate()),
            agent_id=str(AgentId.generate()),
            display_name="my docker ws",
            provider_kind="docker",
            hosting_device_id=record_store.device_id,
            state=RECORD_STATE_ACTIVE,
        ),
    )
    cli.remove_account(user_id)
    session_store.invalidate_identity_cache()

    payload = _build_providers_state_payload(MngrCliBackendResolver(), session_store, None, None)

    (signed_out_section, _local_section) = payload.accounts
    assert signed_out_section.user_id == user_id and signed_out_section.email == email
    assert signed_out_section.state is AccountPanelState.SIGNED_OUT
    assert signed_out_section.imbue_cloud is None and signed_out_section.connections == ()


# _build_workspace_list backend-unreachable verdict


def _make_workspace_agent(provider_name: str, extra_labels: dict[str, str] | None = None) -> DiscoveredAgent:
    """A primary machine agent (carries the machine + is_primary labels)."""
    labels = {"workspace": "my-workspace", "is_primary": "true", **(extra_labels or {})}
    return DiscoveredAgent(
        host_id=HostId("host-" + "0" * 31 + "1"),
        agent_id=AgentId("agent-" + "0" * 31 + "1"),
        agent_name=AgentName("ws-agent"),
        provider_name=ProviderInstanceName(provider_name),
        certified_data={"labels": labels},
    )


def test_build_workspace_list_reports_backend_unreachable_when_its_provider_errored() -> None:
    """A machine whose provider's last poll errored carries ``is_backend_unreachable``; healthy ones do not.

    The row also names its provider, which is what lets the recovery band say
    *which* backend it cannot reach rather than falling back to the generic
    lost-connection copy. The label rides on every row, not just the unreachable
    one: it describes where the machine lives, and the band pairs the two.
    """
    resolver = MngrCliBackendResolver()
    provider_name = "imbue_cloud_acct"
    agent = _make_workspace_agent(provider_name)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    # No provider error -> the backend is not reported unreachable.
    healthy = _build_workspace_list(resolver)
    assert len(healthy) == 1
    assert "is_backend_unreachable" not in healthy[0]
    assert healthy[0]["provider_label"] == "Imbue Cloud"

    # Its provider's latest poll errored -> the backend is unreachable.
    errored = ProviderInstanceName(provider_name)
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={
            errored: DiscoveryError(type_name="RuntimeError", message="boom", provider_name=errored)
        },
        last_snapshot_at=datetime.now(timezone.utc),
    )
    unreachable = _build_workspace_list(resolver)
    assert len(unreachable) == 1
    assert unreachable[0]["is_backend_unreachable"] == "true"
    assert unreachable[0]["provider_label"] == "Imbue Cloud"


def test_build_workspace_list_does_not_report_unreachable_for_unrelated_provider_error() -> None:
    """An error on a different provider must not report a healthy provider's machine unreachable."""
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    other = ProviderInstanceName("some_other_provider")
    seed_provider_snapshots(
        resolver,
        providers=(),
        error_by_provider_name={other: DiscoveryError(type_name="RuntimeError", message="boom", provider_name=other)},
        last_snapshot_at=datetime.now(timezone.utc),
    )
    workspaces = _build_workspace_list(resolver)
    assert len(workspaces) == 1
    assert "is_backend_unreachable" not in workspaces[0]


# _build_workspace_list connectivity locality


@pytest.mark.parametrize("backend", ["local", "docker", "lima"])
def test_build_workspace_list_marks_on_device_machines_as_needing_no_network(backend: str) -> None:
    """The band reads this to know when *not* to blame the network for an outage.

    A machine on one of these backends answers over loopback, so this device
    losing its network says nothing about it -- and the band would otherwise
    displace its recovery copy with "No network connection" and send the user to
    a card that withholds the restart that would fix it.
    """
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent(backend)
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))
    seed_provider_snapshots(resolver, providers=(_make_discovered_provider(backend, backend=backend),))

    workspaces = _build_workspace_list(resolver)

    assert len(workspaces) == 1
    assert workspaces[0]["is_network_dependent"] == "false"


def test_build_workspace_list_marks_a_remote_machine_as_needing_the_network() -> None:
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))
    seed_provider_snapshots(
        resolver, providers=(_make_discovered_provider("imbue_cloud_acct", backend="imbue_cloud"),)
    )

    workspaces = _build_workspace_list(resolver)

    assert workspaces[0]["is_network_dependent"] == "true"


# _build_workspace_list device-cannot-connect verdict


def test_build_workspace_list_reports_only_the_causes_that_never_reached_the_machine() -> None:
    """A failure raised on this device carries ``is_device_cannot_connect``; one that dialed does not.

    The notice band reads this to stop reporting a generic loss of contact for a
    machine that is very likely running fine. The distinction is the whole point:
    a cause that reached the network leaves the machine implicated, and blaming
    the device for it would suppress the restart that fixes it.
    """
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))
    tracker = SystemInterfaceHealthTracker()

    # Nothing observed yet: no verdict to carry.
    assert "is_device_cannot_connect" not in _build_workspace_list(resolver, tracker=tracker)[0]

    # A tunnel this device could not build, raised before any packet was sent.
    tracker.record_connection_failure(
        agent.agent_id, SystemInterfaceBackendFailureReason.TUNNEL_SETUP_FAILED, "no known_hosts"
    )
    assert _build_workspace_list(resolver, tracker=tracker)[0]["is_device_cannot_connect"] == "true"

    # The residual cause, on its own episode: it reached the network and failed
    # there, so the machine is still implicated. (A fresh tracker, because the
    # deference window that governs replacing one cause with another is a
    # separate rule with its own tests.)
    dialed = SystemInterfaceHealthTracker()
    dialed.record_connection_failure(
        agent.agent_id, SystemInterfaceBackendFailureReason.CONNECT_ERROR, "connection refused"
    )
    assert "is_device_cannot_connect" not in _build_workspace_list(resolver, tracker=dialed)[0]


# _build_workspace_list color emission
#
# These assert the SSE workspaces payload carries the stored color.
# Pre-migration workspaces (no ``color`` label) fall back to
# ``DEFAULT_WORKSPACE_COLOR`` so the rollout doesn't visually break
# existing workspaces. The titlebar derives its contrasting foreground
# from the accent in pure CSS (see .titlebar-surface in frontend/src/style.css), so the
# payload no longer carries an ``accent_fg``.


def test_build_workspace_list_emits_stored_color_when_label_present() -> None:
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("docker", extra_labels={"color": "#0b292b"})
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    workspaces = _build_workspace_list(resolver)
    assert len(workspaces) == 1
    assert workspaces[0]["accent"] == "#0b292b"
    assert "accent_fg" not in workspaces[0]


def test_build_workspace_list_falls_back_to_default_color_when_label_missing() -> None:
    """Machines without a ``color`` label (created before the picker
    shipped, or backfilled but not yet written through ``mngr label``)
    render as ``DEFAULT_WORKSPACE_COLOR`` -- the same value new
    machines get pre-selected in the picker."""
    resolver = MngrCliBackendResolver()
    agent = _make_workspace_agent("imbue_cloud_acct")
    resolver.update_agents(ParsedAgentsResult(agent_ids=(agent.agent_id,), discovered_agents=(agent,)))

    workspaces = _build_workspace_list(resolver)
    assert workspaces[0]["accent"] == DEFAULT_WORKSPACE_COLOR
