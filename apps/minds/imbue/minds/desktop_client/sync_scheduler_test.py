"""Unit tests for the sync scheduler's initial-sync (post-signin banner) tracking.

The two-device fixtures mirror test_workspace_sync.py: device A pushes records
into the shared in-memory fake connector; device B signs in fresh and pulls.
"""

import json
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from pydantic import Field
from pydantic import PrivateAttr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.imbue_common.frozen_model import FrozenModel
from imbue.minds.desktop_client.account_settings_store import AccountSettingsStore
from imbue.minds.desktop_client.account_settings_store import SETTINGS_DOCUMENT_KIND
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import SHARED_TEST_HOST_ID
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_fresh_device_scheduler
from imbue.minds.desktop_client.conftest import make_resolver_with_data
from imbue.minds.desktop_client.conftest import make_shared_share
from imbue.minds.desktop_client.conftest import make_shared_with_me_store_for_test
from imbue.minds.desktop_client.conftest import make_sync_test_device
from imbue.minds.desktop_client.conftest import push_remote_workspace_record
from imbue.minds.desktop_client.conftest import seed_shared_with_me_listing
from imbue.minds.desktop_client.forward_identity import ForwardHeadersFile
from imbue.minds.desktop_client.forward_identity import ForwardIdentityPublisher
from imbue.minds.desktop_client.forward_identity import IDENTITY_HEADER
from imbue.minds.desktop_client.mock_agent_creator_test import InFlightAgentCreator
from imbue.minds.desktop_client.mock_agent_creator_test import InFlightCreateAttempt
from imbue.minds.desktop_client.mock_agent_creator_test import make_in_flight_agent_creator
from imbue.minds.desktop_client.shared_with_me_store import SHARED_WITH_ME_DOCUMENT_KIND
from imbue.minds.desktop_client.shared_with_me_store import SharedAccessVerdict
from imbue.minds.desktop_client.shared_with_me_store import SharedWithMeStore
from imbue.minds.desktop_client.sync_scheduler import InitialSyncState
from imbue.minds.desktop_client.sync_scheduler import SyncLoopTiming
from imbue.minds.desktop_client.sync_scheduler import WorkspaceSyncScheduler
from imbue.minds.desktop_client.testing import decode_written_profile_snapshot
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


def test_a_pulled_keybinding_is_materialized_into_the_workspace_snapshot_in_the_same_pass(tmp_path: Path) -> None:
    """The write-back round trip's receiving half (spec section 14): another install saved a keybinding
    through the route and pushed; this install's pass pulls it and rewrites the owned workspace's
    snapshot with it."""
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    pusher_store = AccountSettingsStore(data_dir=tmp_path / "pusher-settings", cli=cli)
    pusher_store.apply_settings_values(user_id, {"keybindings": {"chat": {"send-message": ["ctrl+enter"]}}})
    pusher_store.push_dirty(user_id, email)

    record_store, session_store = make_sync_test_device(tmp_path, f"puller-{uuid4().hex}", cli)
    agent_id = AgentId.generate()
    session_store.associate_created_workspace(
        user_id=user_id,
        agent_id=str(agent_id),
        host_id=f"host-{uuid4().hex}",
        display_name="",
        color=None,
        is_cloud_row=False,
    )
    settings_store = AccountSettingsStore(data_dir=tmp_path / "puller-settings", cli=cli)
    caller = RecordingMngrCaller()
    resolver = StaticBackendResolver(url_by_agent_and_service={str(agent_id): {}})
    profile_writer = WorkspaceProfileWriter(
        mngr_caller=caller,
        backend_resolver=resolver,
        session_store=session_store,
        settings_store=settings_store,
        device_id="device-1",
        hashes_dir=tmp_path / "workspace_profile_hashes",
    )
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=resolver,
        settings_store=settings_store,
        workspace_profile_writer=profile_writer,
    )

    scheduler.run_one_pass()

    snapshot_writes = [call for call in caller.calls if call[0] == "exec"]
    assert len(snapshot_writes) == 1
    document = decode_written_profile_snapshot(snapshot_writes[0][2])
    assert document["account_id"] == user_id
    assert document["keybindings"] == {"chat": {"send-message": ["ctrl+enter"]}}


def test_pass_syncs_workspace_profiles_and_flushes_queued_fact_writes(tmp_path: Path) -> None:
    """Each pass ends with the workspace-boundary steps: the profile rewrite and the fact flush."""
    cli = make_fake_imbue_cloud_cli()
    record_store, session_store = make_sync_test_device(tmp_path, f"boundary-{uuid4().hex}", cli)
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    profile_writer = WorkspaceProfileWriter(
        mngr_caller=RecordingMngrCaller(),
        backend_resolver=resolver,
        session_store=session_store,
        settings_store=None,
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


class _SharedWithMeTestDevice(FrozenModel):
    """One device whose signed-in account has a single share listed at the fake connector."""

    cli: FakeImbueCloudCli = Field(description="The shared fake connector")
    user_id: str = Field(description="The signed-in account's user id")
    email: str = Field(description="The signed-in account's email")
    shared_store: SharedWithMeStore = Field(description="The device's shared-with-me store")
    scheduler: WorkspaceSyncScheduler = Field(description="The device's scheduler, wired to the store")


def _make_shared_with_me_device(base_dir: Path) -> _SharedWithMeTestDevice:
    """A fresh device with one account signed in and one invited share (listing version 1) listed for it."""
    user_id = uuid4().hex
    email = f"user-{uuid4().hex}@example.com"
    cli = make_fake_imbue_cloud_cli()
    cli.add_account(user_id=user_id, email=email)
    seed_shared_with_me_listing(cli, email, make_shared_share(SHARED_TEST_HOST_ID))
    record_store, session_store = make_sync_test_device(base_dir, f"fresh-{uuid4().hex}", cli)
    shared_store = make_shared_with_me_store_for_test(base_dir / "shared", cli)
    scheduler = WorkspaceSyncScheduler(
        record_store=record_store,
        session_store=session_store,
        resolver=make_resolver_with_data(agents_json=json.dumps({"agents": []})),
        shared_with_me_store=shared_store,
    )
    return _SharedWithMeTestDevice(
        cli=cli, user_id=user_id, email=email, shared_store=shared_store, scheduler=scheduler
    )


def test_pass_fetches_the_shared_with_me_listing_only_when_its_match_set_moved_or_the_pass_was_kicked(
    tmp_path: Path,
) -> None:
    device = _make_shared_with_me_device(tmp_path)
    change_signals: list[bool] = []
    device.shared_store.on_change = lambda: change_signals.append(True)

    # No cache yet: the first pass fetches and the home page is told.
    device.scheduler.run_one_pass()
    assert device.cli.shared_with_me_fetch_count == 1
    assert [row.share_key for row in device.shared_store.list_rows(device.user_id)] == [SHARED_TEST_HOST_ID]
    assert change_signals == [True]

    # A quiet tick: the records pull carries the same match set, so nothing is fetched.
    device.scheduler.run_one_pass()
    assert device.cli.shared_with_me_fetch_count == 1

    # The listing changed at the connector: the match set moved, so the pass refetches.
    seed_shared_with_me_listing(device.cli, device.email, version=2)
    device.scheduler.run_one_pass()
    assert device.cli.shared_with_me_fetch_count == 2
    assert device.shared_store.list_rows(device.user_id) == []

    # A kicked pass refetches regardless of the match set, once.
    device.scheduler.kick()
    device.scheduler.run_one_pass()
    device.scheduler.run_one_pass()
    assert device.cli.shared_with_me_fetch_count == 3


def test_a_failed_listing_fetch_keeps_the_cache_and_does_not_fail_the_pass(tmp_path: Path) -> None:
    device = _make_shared_with_me_device(tmp_path)
    device.scheduler.run_one_pass()

    device.cli.is_shared_with_me_failing = True
    seed_shared_with_me_listing(device.cli, device.email, version=2)
    device.scheduler.run_one_pass()

    # The fetch was attempted (the match set moved) and its failure kept the last listing.
    assert device.cli.shared_with_me_fetch_count == 2
    assert [row.share_key for row in device.shared_store.list_rows(device.user_id)] == [SHARED_TEST_HOST_ID]


def test_pass_pushes_a_recorded_verdict_and_pulls_one_another_device_recorded(tmp_path: Path) -> None:
    device = _make_shared_with_me_device(tmp_path)
    device.scheduler.run_one_pass()
    change_signals: list[bool] = []
    device.shared_store.on_change = lambda: change_signals.append(True)
    # Another device opened the share and recorded what it learned.
    pusher_store = make_shared_with_me_store_for_test(tmp_path / "pusher", device.cli)
    pusher_store.refresh(device.user_id, device.email, datetime.now(timezone.utc))
    pusher_store.record_verdict(
        device.user_id, SHARED_TEST_HOST_ID, SharedAccessVerdict.REFUSED, datetime.now(timezone.utc)
    )
    pusher_store.documents.push_dirty(device.user_id, device.email)

    device.scheduler.run_one_pass()
    row = device.shared_store.find_row(device.user_id, SHARED_TEST_HOST_ID)
    assert row is not None and row.verdict is SharedAccessVerdict.REFUSED
    # The pulled verdict reaches the home page like a local write would; the
    # listing itself was not refetched (a quiet tick), so that is the one signal.
    assert change_signals == [True]

    # This device's retry clears it; the next pass pushes the cleared entry.
    device.shared_store.clear_verdict(device.user_id, SHARED_TEST_HOST_ID)
    device.scheduler.run_one_pass()
    pushed = device.cli.sync_documents_by_email[device.email][SHARED_WITH_ME_DOCUMENT_KIND][SHARED_TEST_HOST_ID][
        "metadata"
    ]
    assert isinstance(pushed, dict)
    assert pushed["last_verdict"] is None


def test_signed_out_account_loses_its_shared_with_me_cache(tmp_path: Path) -> None:
    device = _make_shared_with_me_device(tmp_path)
    device.scheduler.run_one_pass()
    assert device.shared_store.list_cached_user_ids() == [device.user_id]

    device.cli.remove_account(device.user_id)
    device.scheduler.session_store.invalidate_identity_cache()
    device.scheduler.run_one_pass()

    assert device.shared_store.list_cached_user_ids() == []


def test_a_failed_account_listing_keeps_every_shared_with_me_cache(tmp_path: Path) -> None:
    # An empty account listing the identity read could not produce is not a
    # sign-out: the caches (and the home page's section) stay as they were.
    device = _make_shared_with_me_device(tmp_path)
    device.scheduler.run_one_pass()
    change_signals: list[bool] = []
    device.shared_store.on_change = lambda: change_signals.append(True)

    device.cli.is_auth_list_failing = True
    device.scheduler.session_store.invalidate_identity_cache()
    device.scheduler.run_one_pass()

    assert device.shared_store.list_cached_user_ids() == [device.user_id]
    assert change_signals == []


class _CountingPassScheduler(WorkspaceSyncScheduler):
    """Scheduler whose pass only records that it ran, to observe when the loop runs passes."""

    _passes: threading.Semaphore = PrivateAttr(default_factory=lambda: threading.Semaphore(0))

    def run_one_pass(self) -> None:
        self._passes.release()


def _make_counting_scheduler(
    tmp_path: Path, creator: InFlightAgentCreator, timing: SyncLoopTiming
) -> _CountingPassScheduler:
    record_store, session_store = make_sync_test_device(tmp_path, f"hold-{uuid4().hex}", make_fake_imbue_cloud_cli())
    resolver = make_resolver_with_data(agents_json=json.dumps({"agents": []}))
    return _CountingPassScheduler(
        record_store=record_store, session_store=session_store, resolver=resolver, agent_creator=creator, timing=timing
    )


def _make_live_creator(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> InFlightAgentCreator:
    attempt = InFlightCreateAttempt(provider_instance_name="imbue_cloud_user", host_name="workspace-1")
    return make_in_flight_agent_creator(tmp_path, root_concurrency_group, (attempt,))


def test_periodic_pass_waits_for_the_create_and_its_settle_window(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    creator = _make_live_creator(tmp_path, root_concurrency_group)
    timing = SyncLoopTiming(
        tick_seconds=0.05, create_hold_poll_seconds=0.01, post_create_settle_seconds=1.0, max_create_hold_seconds=60.0
    )
    scheduler = _make_counting_scheduler(tmp_path, creator, timing)

    with ConcurrencyGroup(name="test-sync-hold") as concurrency_group:
        scheduler.start(concurrency_group)
        try:
            assert scheduler._passes.acquire(timeout=5.0)

            # Many ticks elapse while the create runs; none of them runs a pass.
            assert not scheduler._passes.acquire(timeout=0.5)

            creator.in_flight_attempts = ()
            # The settle window counts from the last poll that saw the create, which a busy machine can leave
            # well behind the create's end, so only the window's first half is held to running no pass.
            assert not scheduler._passes.acquire(timeout=timing.post_create_settle_seconds / 2)
            assert scheduler._passes.acquire(timeout=5.0)
        finally:
            scheduler.stop()


def test_kick_runs_a_pass_while_a_create_is_in_flight(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    creator = _make_live_creator(tmp_path, root_concurrency_group)
    timing = SyncLoopTiming(tick_seconds=0.05, create_hold_poll_seconds=0.01, max_create_hold_seconds=60.0)
    scheduler = _make_counting_scheduler(tmp_path, creator, timing)

    with ConcurrencyGroup(name="test-sync-hold-kick") as concurrency_group:
        scheduler.start(concurrency_group)
        try:
            assert scheduler._passes.acquire(timeout=5.0)
            assert not scheduler._passes.acquire(timeout=0.3)

            scheduler.kick()
            assert scheduler._passes.acquire(timeout=5.0)
        finally:
            scheduler.stop()


def test_periodic_pass_hold_ends_after_the_maximum_even_if_the_create_still_runs(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    creator = _make_live_creator(tmp_path, root_concurrency_group)
    timing = SyncLoopTiming(tick_seconds=0.05, create_hold_poll_seconds=0.01, max_create_hold_seconds=0.3)
    scheduler = _make_counting_scheduler(tmp_path, creator, timing)

    with ConcurrencyGroup(name="test-sync-hold-max") as concurrency_group:
        scheduler.start(concurrency_group)
        try:
            assert scheduler._passes.acquire(timeout=5.0)
            first_pass_at = time.monotonic()
            assert scheduler._passes.acquire(timeout=5.0)
            assert time.monotonic() - first_pass_at >= timing.max_create_hold_seconds
        finally:
            scheduler.stop()
