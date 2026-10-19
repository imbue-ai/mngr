import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from imbue.minds.desktop_client.backend_resolver import BackendResolverInterface
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import associate_hosted_workspace
from imbue.minds.desktop_client.conftest import make_agents_json
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.data_types import ShareRemovalOutcome
from imbue.minds.desktop_client.data_types import SharedWorkspaceRecordState
from imbue.minds.desktop_client.imbue_cloud_cli import ShareCliInfo
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.shared_workspaces import build_shared_workspace_rows
from imbue.minds.desktop_client.shared_workspaces import clear_materials_of_shares_retired_elsewhere
from imbue.minds.desktop_client.shared_workspaces import describe_last_seen
from imbue.minds.desktop_client.shared_workspaces import remove_account_share
from imbue.minds.desktop_client.workspace_record_store import ReplicaRecord
from imbue.minds.utils.mngr_caller import MngrCallResult
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_NOW = datetime(2026, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
_EMAIL = "owner@example.com"
_USER_ID = "11111111-1111-1111-1111-111111111111"


def _share(
    host_id: str, workspace_id: str | None, last_seen_at: str | None = None, state: str = "active"
) -> ShareCliInfo:
    return ShareCliInfo(
        host_id=host_id,
        workspace_id=workspace_id,
        workspace_domain=f"{host_id[:12]}.owner1234.us1.shares.example",
        region="us1",
        state=state,
        created_at="2026-09-01 10:00:00+00:00",
        last_seen_at=last_seen_at,
    )


def test_describe_last_seen_reads_a_fresh_stamp_as_connected_and_an_old_one_in_days() -> None:
    assert describe_last_seen(None, _NOW) == "Never connected"
    assert describe_last_seen(str(_NOW - timedelta(minutes=5)), _NOW) == "Connected now"
    assert describe_last_seen(str(_NOW - timedelta(minutes=40)), _NOW) == "Last connected 1 hour ago"
    assert describe_last_seen(str(_NOW - timedelta(hours=1)), _NOW) == "Last connected 1 hour ago"
    assert describe_last_seen(str(_NOW - timedelta(hours=5)), _NOW) == "Last connected 5 hours ago"
    assert describe_last_seen(str(_NOW - timedelta(days=1, hours=3)), _NOW) == "Last connected 1 day ago"
    assert describe_last_seen(str(_NOW - timedelta(days=9)), _NOW) == "Last connected 9 days ago"
    assert describe_last_seen("not a timestamp", _NOW) == "Never connected"


def test_build_rows_joins_each_share_to_its_record_and_flags_the_leaked_ones() -> None:
    live_host, gone_host, orphan_host, legacy_host, inactive_host = (f"host-{digit * 32}" for digit in "abcde")
    records = [
        ReplicaRecord(host_id=live_host, agent_id="agent-live", display_name="alpha", device_label="laptop"),
        ReplicaRecord(host_id=gone_host, agent_id="agent-gone", display_name="beta", state="destroyed"),
        ReplicaRecord(host_id=legacy_host, agent_id="agent-legacy", display_name="gamma", device_label="desk"),
    ]
    shares = [
        _share(live_host, "agent-live", last_seen_at=str(_NOW - timedelta(minutes=2))),
        _share(gone_host, "agent-gone", last_seen_at=str(_NOW - timedelta(days=30))),
        _share(orphan_host, "agent-orphan"),
        _share(legacy_host, None),
        _share(inactive_host, "agent-old", state="inactive"),
    ]

    rows = build_shared_workspace_rows(shares, records, _EMAIL, {"agent-live"}, _NOW)

    assert [row.host_id for row in rows] == [live_host, gone_host, orphan_host, legacy_host]
    by_host = {row.host_id: row for row in rows}
    assert (by_host[live_host].display_name, by_host[live_host].device_label) == ("alpha", "This device")
    assert by_host[live_host].record_state is SharedWorkspaceRecordState.ACTIVE
    assert by_host[live_host].is_hosted_here is True
    assert by_host[live_host].is_likely_leaked is False
    assert by_host[live_host].last_seen_display == "Connected now"
    assert by_host[live_host].created_display == "2026-09-01"
    assert by_host[gone_host].record_state is SharedWorkspaceRecordState.DESTROYED
    assert by_host[gone_host].is_likely_leaked is True
    assert by_host[gone_host].last_seen_display == "Last connected 30 days ago"
    assert by_host[gone_host].device_label == "Unknown device"
    assert by_host[orphan_host].record_state is SharedWorkspaceRecordState.MISSING
    assert by_host[orphan_host].display_name == shares[2].workspace_domain
    assert by_host[orphan_host].is_likely_leaked is True
    # A legacy row pairs through the host it names and takes that record's workspace.
    assert (by_host[legacy_host].workspace_id, by_host[legacy_host].display_name) == ("agent-legacy", "gamma")
    assert by_host[legacy_host].record_state is SharedWorkspaceRecordState.ACTIVE


def test_build_rows_pairs_a_legacy_share_with_the_hosts_active_record_over_a_destroyed_one() -> None:
    reused_host = "host-" + "f" * 32
    records = [
        ReplicaRecord(host_id=reused_host, agent_id="agent-before", display_name="before", state="destroyed"),
        ReplicaRecord(host_id=reused_host, agent_id="agent-after", display_name="after"),
    ]

    (row,) = build_shared_workspace_rows([_share(reused_host, None)], records, _EMAIL, set(), _NOW)

    assert (row.workspace_id, row.record_state) == ("agent-after", SharedWorkspaceRecordState.ACTIVE)


def _cli_with_share(host_id: str, workspace_id: str | None) -> FakeImbueCloudCli:
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    cli.add_share(_EMAIL, host_id)
    cli.share_details_by_host_id[host_id] = _share(host_id, workspace_id)
    return cli


def _signed_in_device(
    tmp_path: Path, agents_json: str
) -> tuple[FakeImbueCloudCli, MultiAccountSessionStore, BackendResolverInterface]:
    """A device with the test account signed in and discovering the workspaces in ``agents_json``."""
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=_USER_ID, email=_EMAIL)
    store = make_session_store_for_test(tmp_path, cli=cli)
    return cli, store, make_resolver_with_data(agents_json)


def test_remove_account_share_deletes_a_share_whose_workspace_is_not_here(tmp_path: Path) -> None:
    host_id = "host-" + "a" * 32
    cli = _cli_with_share(host_id, "agent-gone")
    store = make_session_store_for_test(tmp_path, cli=cli)
    resolver = StaticBackendResolver(url_by_agent_and_service={})

    outcome = remove_account_share(_EMAIL, host_id, cli, store, resolver, None)

    assert outcome is ShareRemovalOutcome.REMOVED
    assert cli.deleted_share_host_ids == [host_id]
    # Idempotent: the share is gone, so a second removal has nothing to do and still succeeds.
    assert remove_account_share(_EMAIL, host_id, cli, store, resolver, None) is ShareRemovalOutcome.REMOVED
    assert cli.deleted_share_host_ids == [host_id]


def test_remove_account_share_unpublishes_a_workspace_this_device_runs(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    cli, store, resolver = _signed_in_device(tmp_path, make_agents_json(agent_id))
    host_id = associate_hosted_workspace(store, agent_id, resolver, _USER_ID)
    cli.add_share(_EMAIL, host_id)

    outcome = remove_account_share(_EMAIL, host_id, cli, store, resolver, None)

    assert outcome is ShareRemovalOutcome.STOPPED_SHARING
    assert cli.deleted_share_host_ids == [host_id]
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    # The unpublish took the materials out of the workspace before deleting the share.
    assert any(call and call[0] == "exec" and "share.env" in call[2] for call in caller.calls)


def test_remove_account_share_deletes_the_accounts_share_of_a_hosted_workspace_it_does_not_own(
    tmp_path: Path,
) -> None:
    # An unpublish acts for the workspace's own account; with no account
    # associated it would refuse, and the row could never be removed.
    agent_id = AgentId.generate()
    cli, store, resolver = _signed_in_device(tmp_path, make_agents_json(agent_id))
    display_info = resolver.get_agent_display_info(agent_id)
    assert display_info is not None
    host_id = str(display_info.host_id)
    cli.add_share(_EMAIL, host_id)

    outcome = remove_account_share(_EMAIL, host_id, cli, store, resolver, None)

    assert outcome is ShareRemovalOutcome.REMOVED
    assert cli.deleted_share_host_ids == [host_id]
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    assert [call for call in caller.calls if call and call[0] == "exec"] == []


def test_remove_account_share_retires_a_suspended_share_of_a_workspace_this_device_runs(tmp_path: Path) -> None:
    # The page lists a suspended share so its owner can remove it; the hosted
    # path has to delete it too, or the row would outlive every "Stop sharing".
    agent_id = AgentId.generate()
    cli, store, resolver = _signed_in_device(tmp_path, make_agents_json(agent_id))
    host_id = associate_hosted_workspace(store, agent_id, resolver, _USER_ID)
    cli.shares_by_account[_EMAIL] = {host_id: "suspended"}

    outcome = remove_account_share(_EMAIL, host_id, cli, store, resolver, None)

    assert outcome is ShareRemovalOutcome.STOPPED_SHARING
    assert cli.deleted_share_host_ids == [host_id]


def test_clear_materials_of_shares_retired_elsewhere_acts_only_on_hosted_workspaces_the_connector_confirms(
    tmp_path: Path,
) -> None:
    retired_agent_id = AgentId.generate()
    still_shared_agent_id = AgentId.generate()
    # Two workspaces on two machines (the canned agents JSON would put them on one).
    cli, store, resolver = _signed_in_device(
        tmp_path,
        json.dumps(
            {
                "agents": [
                    {"id": str(agent_id), "labels": {"is_primary": "true"}, "host": {"id": str(HostId.generate())}}
                    for agent_id in (retired_agent_id, still_shared_agent_id)
                ]
            }
        ),
    )
    still_shared_info = resolver.get_agent_display_info(still_shared_agent_id)
    assert still_shared_info is not None
    host_id = str(still_shared_info.host_id)
    # The pass says both dropped out of the shared set; the connector still holds the second one's share.
    cli.add_share(_EMAIL, host_id)

    cleared = clear_materials_of_shares_retired_elsewhere(
        {_USER_ID: [str(retired_agent_id), str(still_shared_agent_id), str(AgentId.generate())]},
        cli,
        store,
        resolver,
    )

    assert cleared == [str(retired_agent_id)]
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    exec_commands = [call[2] for call in caller.calls if call and call[0] == "exec"]
    assert len(exec_commands) == 1 and "share.env" in exec_commands[0]


def test_clear_materials_of_shares_retired_elsewhere_does_not_count_a_workspace_whose_exec_failed(
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    cli, store, resolver = _signed_in_device(tmp_path, make_agents_json(agent_id))
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    caller.result = MngrCallResult(returncode=1, stderr="container is not running")

    cleared = clear_materials_of_shares_retired_elsewhere({_USER_ID: [str(agent_id)]}, cli, store, resolver)

    # The connector confirmed the share is gone, so the removal was attempted, but a failed exec is not a clear.
    assert len([call for call in caller.calls if call and call[0] == "exec"]) == 1
    assert cleared == []


def test_clear_materials_of_shares_retired_elsewhere_leaves_everything_when_the_connector_is_unreachable(
    tmp_path: Path,
) -> None:
    agent_id = AgentId.generate()
    cli, store, resolver = _signed_in_device(tmp_path, make_agents_json(agent_id))
    cli.is_share_lookup_failing = True

    cleared = clear_materials_of_shares_retired_elsewhere({_USER_ID: [str(agent_id)]}, cli, store, resolver)

    assert cleared == []
    caller = cli.mngr_caller
    assert isinstance(caller, RecordingMngrCaller)
    assert [call for call in caller.calls if call and call[0] == "exec"] == []
