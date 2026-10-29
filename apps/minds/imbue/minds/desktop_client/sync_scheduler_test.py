"""Unit tests for the sync scheduler's initial-sync (post-signin banner) tracking.

The two-device fixtures mirror test_workspace_sync.py: device A pushes records
into the shared in-memory fake connector; device B signs in fresh and pulls.
"""

import json
import threading
from pathlib import Path
from uuid import uuid4

from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.account_settings_store import SETTINGS_DOCUMENT_KIND
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_fresh_device_scheduler
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_sync_test_device
from imbue.minds.desktop_client.conftest import push_remote_workspace_record
from imbue.minds.desktop_client.forward_identity import ForwardHeadersFile
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.forward_identity import IDENTITY_HEADER
from imbue.minds.desktop_client.sync_scheduler import InitialSyncState
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.workspace_profile_writer import WorkspaceProfileWriter
from imbue.minds.utils.testing import RecordingMngrCaller
from imbue.mngr.primitives import AgentId


def test_note_account_signin_marks_pending_only_when_no_local_records(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    # A device that already pulled the account's records tracks nothing.
    scheduler_with_records = make_fresh_device_scheduler(tmp_path, cli)
    scheduler_with_records.run_one_pass()
    scheduler_with_records.note_account_signin(user_id, email)
    assert scheduler_with_records.list_initial_sync_statuses() == []

    # A record-less device tracks the signin as PENDING.
    fresh_scheduler = make_fresh_device_scheduler(tmp_path, cli)
    fresh_scheduler.note_account_signin(user_id, email)
    statuses = fresh_scheduler.list_initial_sync_statuses()
    assert len(statuses) == 1
    assert statuses[0].state == InitialSyncState.PENDING
    assert statuses[0].email == email


def test_initial_sync_resolves_to_done_with_remote_workspace_count(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    scheduler.run_one_pass()

    statuses = scheduler.list_initial_sync_statuses()
    assert len(statuses) == 1
    assert statuses[0].state == InitialSyncState.DONE
    assert statuses[0].workspace_count == 1


def test_failed_pass_marks_pending_failed_then_recovers(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)

    # A connector outage marks the pending fetch FAILED (and must not raise).
    cli.is_sync_offline = True
    scheduler.run_one_pass_guarded()
    statuses_after_failure = scheduler.list_initial_sync_statuses()
    assert len(statuses_after_failure) == 1
    assert statuses_after_failure[0].state == InitialSyncState.FAILED
    assert statuses_after_failure[0].error is not None

    # The next successful pass flips it to DONE.
    cli.is_sync_offline = False
    scheduler.run_one_pass_guarded()
    statuses_after_recovery = scheduler.list_initial_sync_statuses()
    assert len(statuses_after_recovery) == 1
    assert statuses_after_recovery[0].state == InitialSyncState.DONE
    assert statuses_after_recovery[0].workspace_count == 1


def test_signed_out_account_entry_dropped_on_next_pass(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)

    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    assert len(scheduler.list_initial_sync_statuses()) == 1

    cli.remove_account(user_id)
    scheduler.session_store.invalidate_identity_cache()
    scheduler.run_one_pass()
    assert scheduler.list_initial_sync_statuses() == []


class _BlockingPassScheduler(WorkspaceSyncScheduler):
    """Scheduler whose pass blocks until released, to exercise stop() ordering."""

    _pass_started: threading.Event = PrivateAttr(default_factory=threading.Event)
    _pass_release: threading.Event = PrivateAttr(default_factory=threading.Event)

    def run_one_pass(self) -> None:
        self._pass_started.set()
        self._pass_release.wait(timeout=5.0)


def test_brand_new_account_resolves_to_done_with_zero_workspaces(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)

    scheduler = make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    scheduler.run_one_pass()

    statuses = scheduler.list_initial_sync_statuses()
    assert len(statuses) == 1
    assert statuses[0].state == InitialSyncState.DONE
    assert statuses[0].workspace_count == 0


def test_pass_rebuilds_the_forward_headers_file_from_the_pulled_shared_workspaces(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    shared_agent_id = str(AgentId.generate())
    cli.shared_agent_ids_by_email[email] = {shared_agent_id}
    record_store, session_store = make_sync_test_device(tmp_path, f"headers-{uuid4().hex}", cli)
    headers_file = ForwardHeadersFile(path=tmp_path / "forward_headers.json")
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=make_resolver_with_data(agents_json=json.dumps({"agents": []})),
        forward_identity=ForwardIdentityPublisher(session_store=session_store, headers_file=headers_file),
    )

    scheduler.run_one_pass()

    written = json.loads(headers_file.path.read_text())
    assert sorted(written) == sorted(["*", shared_agent_id])
    assert json.loads(written[shared_agent_id][IDENTITY_HEADER]) == {"owner": True, "user_id": user_id, "email": email}

    # A share disabled elsewhere drops out on the next pass; a pull that fails
    # to reach the connector leaves the last known set in place.
    cli.shared_agent_ids_by_email[email] = set()
    cli.is_sync_offline = True
    scheduler.run_one_pass_guarded()
    assert shared_agent_id in json.loads(headers_file.path.read_text())
    cli.is_sync_offline = False
    scheduler.run_one_pass()
    assert list(json.loads(headers_file.path.read_text())) == ["*"]


def test_pass_reports_the_shares_retired_elsewhere_to_its_hook(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    shared_agent_id = str(AgentId.generate())
    cli.shared_agent_ids_by_email[email] = {shared_agent_id}
    record_store, session_store = make_sync_test_device(tmp_path, f"retired-{uuid4().hex}", cli)
    reported: list[dict[str, list[str]]] = []
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=make_resolver_with_data(agents_json=json.dumps({"agents": []})),
        forward_identity=ForwardIdentityPublisher(
            session_store=session_store, headers_file=ForwardHeadersFile(path=tmp_path / "forward_headers.json")
        ),
        on_shares_retired_elsewhere=lambda dropped: reported.append({k: list(v) for k, v in dropped.items()}),
    )

    scheduler.run_one_pass()
    assert reported == []

    cli.shared_agent_ids_by_email[email] = set()
    scheduler.run_one_pass()
    assert reported == [{user_id: [shared_agent_id]}]

    # Nothing changed since: the hook is not called for an unchanged set.
    scheduler.run_one_pass()
    assert len(reported) == 1


def test_stop_blocks_until_in_flight_pass_finishes(tmp_path: Path) -> None:
    # Regression test for the shutdown race: stop() must not return while a
    # pass is mid-flight, so the caller can safely tear down the shared mngr
    # caller the pass runs through without racing it.
    cli = make_fake_imbue_cloud_cli()
    record_store, session_store = make_sync_test_device(tmp_path, f"blocking-{uuid4().hex}", cli)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    scheduler = _BlockingPassScheduler(record_store=record_store, session_store=session_store, resolver=resolver)

    with ConcurrencyGroup(name="test-sync-stop") as concurrency_group:
        scheduler.start(concurrency_group)
        # Wait for the loop to enter a pass and block inside it.
        assert scheduler._pass_started.wait(timeout=5.0)

        stop_returned = threading.Event()
        stop_thread = threading.Thread(target=lambda: (scheduler.stop(), stop_returned.set()), name="stop-caller")
        stop_thread.start()

        # stop() must NOT return while the pass is still in flight.
        assert not stop_returned.wait(timeout=0.5)

        # Releasing the pass lets the loop observe the stop signal and exit,
        # at which point stop() returns.
        scheduler._pass_release.set()
        assert stop_returned.wait(timeout=5.0)
        stop_thread.join(timeout=5.0)


def test_pass_pulls_and_pushes_the_settings_document_and_reports_a_pulled_change(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    # Another device already pushed its settings document.
    pusher_store = AccountSettingsStore(data_dir=tmp_path / "pusher-settings", cli=cli)
    pusher_store.apply_settings_values(user_id, {"auto_update_workspaces": True})
    pusher_store.push_dirty(user_id, email)
    record_store, session_store = make_sync_test_device(tmp_path, f"fresh-{uuid4().hex}", cli)
    settings_store = AccountSettingsStore(data_dir=tmp_path / "fresh-settings", cli=cli)
    change_signals: list[bool] = []
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=make_resolver_with_data(agents_json=json.dumps({"agents": []})),
        settings_store=settings_store,
    )
    scheduler.on_account_settings_changed = lambda: change_signals.append(True)

    scheduler.run_one_pass()

    # The pull landed the other device's document, with a local edit's side effects.
    assert settings_store.read_settings(user_id).auto_update_workspaces is True
    assert change_signals == [True]

    # A local edit is pushed by the next pass; nothing pulled changed, so no new signal.
    settings_store.apply_settings_values(user_id, {"notifications_enabled": False})
    scheduler.run_one_pass()

    pushed = cli.sync_documents_by_email[email][SETTINGS_DOCUMENT_KIND]["default"]["metadata"]
    assert isinstance(pushed, dict)
    assert pushed["notifications_enabled"] is False
    assert change_signals == [True]


def test_pass_syncs_workspace_profiles_and_flushes_queued_fact_writes(tmp_path: Path) -> None:
    """Each pass ends with the workspace-boundary steps: the profile rewrite and the fact flush."""
    cli = make_fake_imbue_cloud_cli()
    record_store, session_store = make_sync_test_device(tmp_path, f"boundary-{uuid4().hex}", cli)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    profile_writer = WorkspaceProfileWriter(
        mngr_caller=RecordingMngrCaller(),
        backend_resolver=resolver,
        session_store=session_store,
        device_id="device-1",
        hashes_dir=tmp_path / "workspace_profile_hashes",
    )
    sync_calls: list[bool] = []
    flush_calls: list[bool] = []

    class _CountingWriter(WorkspaceProfileWriter):
        def sync_profiles(self) -> None:
            sync_calls.append(True)

    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=resolver,
        workspace_profile_writer=_CountingWriter(**dict(profile_writer)),
        flush_workspace_fact_writes=lambda: flush_calls.append(True),
    )

    scheduler.run_one_pass()

    assert sync_calls == [True]
    assert flush_calls == [True]
