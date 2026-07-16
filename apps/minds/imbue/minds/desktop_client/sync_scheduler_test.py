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
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.sync_scheduler import InitialSyncState
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.workspace_record_store import WorkspaceRecordStore
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId


def _make_device(
    base: Path, name: str, cli: FakeImbueCloudCli
) -> tuple[WorkspaceRecordStore, MultiAccountSessionStore]:
    paths = WorkspacePaths(data_dir=base / name)
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    record_store = WorkspaceRecordStore(
        paths=paths,
        cli=cli,
        device_id=f"device-{name}",
        device_label=name,
    )
    session_store = MultiAccountSessionStore(data_dir=paths.data_dir, cli=cli, record_store=record_store)
    return record_store, session_store


def _make_fresh_device_scheduler(base: Path, cli: FakeImbueCloudCli) -> WorkspaceSyncScheduler:
    record_store, session_store = _make_device(base, f"fresh-{uuid4().hex}", cli)
    empty_resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    return WorkspaceSyncScheduler(record_store=record_store, session_store=session_store, resolver=empty_resolver)


def _push_remote_workspace_record(base: Path, cli: FakeImbueCloudCli, user_id: str, name: str) -> None:
    """Simulate another device having synced one active workspace for the account."""
    _, session_store = _make_device(base, f"pusher-{uuid4().hex}", cli)
    agent_id = AgentId.generate()
    host_id = HostId.generate()
    agents = [{"id": str(agent_id), "labels": {"is_primary": "true"}, "host": {"id": str(host_id), "name": name}}]
    resolver: MngrCliBackendResolver = make_resolver_with_data(agents_json=json.dumps({"agents": agents}))
    session_store.associate_workspace(user_id, str(agent_id), resolver)


def test_note_account_signin_marks_pending_only_when_no_local_records(tmp_path: Path) -> None:
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    _push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    # A device that already pulled the account's records tracks nothing.
    scheduler_with_records = _make_fresh_device_scheduler(tmp_path, cli)
    scheduler_with_records.run_one_pass()
    scheduler_with_records.note_account_signin(user_id, email)
    assert scheduler_with_records.list_initial_sync_statuses() == []

    # A record-less device tracks the signin as PENDING.
    fresh_scheduler = _make_fresh_device_scheduler(tmp_path, cli)
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
    _push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    scheduler = _make_fresh_device_scheduler(tmp_path, cli)
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
    _push_remote_workspace_record(tmp_path, cli, user_id, "ws-1")

    scheduler = _make_fresh_device_scheduler(tmp_path, cli)
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

    scheduler = _make_fresh_device_scheduler(tmp_path, cli)
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

    scheduler = _make_fresh_device_scheduler(tmp_path, cli)
    scheduler.note_account_signin(user_id, email)
    scheduler.run_one_pass()

    statuses = scheduler.list_initial_sync_statuses()
    assert len(statuses) == 1
    assert statuses[0].state == InitialSyncState.DONE
    assert statuses[0].workspace_count == 0


def test_stop_blocks_until_in_flight_pass_finishes(tmp_path: Path) -> None:
    # Regression test for the shutdown race: stop() must not return while a
    # pass is mid-flight, so the caller can safely tear down the shared mngr
    # caller the pass runs through without racing it.
    cli = make_fake_imbue_cloud_cli()
    record_store, session_store = _make_device(tmp_path, f"blocking-{uuid4().hex}", cli)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    scheduler = _BlockingPassScheduler(record_store=record_store, session_store=session_store, resolver=resolver)

    with ConcurrencyGroup(name="test-sync-stop") as concurrency_group:
        scheduler.start(concurrency_group)
        # Wait for the loop to enter a pass and block inside it.
        assert scheduler._pass_started.wait(timeout=5.0)

        stop_returned = threading.Event()
        stop_thread = threading.Thread(
            target=lambda: (scheduler.stop(), stop_returned.set()), name="stop-caller"
        )
        stop_thread.start()

        # stop() must NOT return while the pass is still in flight.
        assert not stop_returned.wait(timeout=0.5)

        # Releasing the pass lets the loop observe the stop signal and exit,
        # at which point stop() returns.
        scheduler._pass_release.set()
        assert stop_returned.wait(timeout=5.0)
        stop_thread.join(timeout=5.0)
