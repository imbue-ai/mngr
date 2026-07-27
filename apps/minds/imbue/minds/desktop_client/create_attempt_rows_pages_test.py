"""Integration tests for the create attempt rows UX.

Covers the workspace-list payload merge (create attempt entries alongside real
workspaces), the landing page's create attempt cards, the re-enterable
``/creating/<id>`` page's record-backed fallbacks, the ``/create?retry=<id>``
pre-fill, and the create attempts discard / dismiss API routes.
"""

import json
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from flask.testing import FlaskClient

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.agent_creator import CreateAttemptLogSink
from imbue.minds.desktop_client.agent_creator import LOG_SENTINEL
from imbue.minds.desktop_client.app import _build_workspace_list
from imbue.minds.desktop_client.app import _visible_create_attempt_rows
from imbue.minds.desktop_client.app import create_desktop_client
from imbue.minds.desktop_client.auth import FileAuthStore
from imbue.minds.desktop_client.backend_resolver import StaticBackendResolver
from imbue.minds.desktop_client.cookie_manager import SESSION_COOKIE_NAME
from imbue.minds.desktop_client.cookie_manager import create_session_cookie
from imbue.minds.desktop_client.create_attempt_discard import CreateAttemptDiscardStatus
from imbue.minds.desktop_client.create_attempt_discard import read_discard
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.state import get_state
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import CreateAttemptId
from imbue.minds.primitives import LaunchMode


def _create_attempt_id() -> str:
    return f"create-attempt-{uuid4().hex}"


def _record(
    create_attempt_id: str,
    state: PendingCreateAttemptState,
    *,
    provider_instance_name: str = "modal",
    launch_mode: LaunchMode = LaunchMode.MODAL,
    host_name: str = "row-test-name",
    display_name: str = "Row Test Name",
    error: str | None = None,
    error_kind: str | None = None,
    log_tail: tuple[str, ...] = (),
    cloud_account: str = "",
    instance_type: str = "",
) -> PendingCreateAttemptRecord:
    now = datetime.now(timezone.utc)
    return PendingCreateAttemptRecord(
        create_attempt_id=create_attempt_id,
        state=state,
        provider_instance_name=provider_instance_name,
        created_at=now,
        updated_at=now,
        error=error,
        error_kind=error_kind,
        log_tail=log_tail,
        request=PendingCreateAttemptRequest(
            repo_source="https://example.com/some-repo.git",
            host_name=host_name,
            display_name=display_name,
            branch="feature-branch-7",
            launch_mode=launch_mode,
            account_email="owner@example.com",
            color="#a1b2c3",
            backup_api_key_env="",
            cloud_account=cloud_account,
            instance_type=instance_type,
        ),
    )


class _FixedLiveCreateAttemptAgentCreator(AgentCreator):
    """Creator reporting one fixed create attempt id as live, for the 409-guard tests."""

    live_create_attempt_id_str: str

    def live_in_flight_create_attempt_ids(self) -> set[str]:
        return {self.live_create_attempt_id_str}


def _make_client_with_store(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
    live_create_attempt_id_str: str | None = None,
    mngr_binary: str = "mngr",
) -> tuple[FlaskClient, PendingCreateAttemptStore, AgentCreator]:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    creator: AgentCreator
    if live_create_attempt_id_str is None:
        creator = AgentCreator(
            paths=WorkspacePaths(data_dir=tmp_path / "minds"),
            root_concurrency_group=root_concurrency_group,
            notification_dispatcher=notification_dispatcher,
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
            pending_create_attempt_store=store,
        )
    else:
        creator = _FixedLiveCreateAttemptAgentCreator(
            live_create_attempt_id_str=live_create_attempt_id_str,
            paths=WorkspacePaths(data_dir=tmp_path / "minds"),
            root_concurrency_group=root_concurrency_group,
            notification_dispatcher=notification_dispatcher,
            system_interface_health_tracker=SystemInterfaceHealthTracker(),
            pending_create_attempt_store=store,
        )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        agent_creator=creator,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        mngr_binary=mngr_binary,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))
    return client, store, creator


def test_workspace_list_payload_carries_create_attempt_entries(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    interrupted_id = _create_attempt_id()
    store.write_record(_record(interrupted_id, PendingCreateAttemptState.IN_FLIGHT))

    with client.application.test_request_context("/"):
        resolver = get_state().backend_resolver
        entries = _build_workspace_list(resolver, create_attempt_rows=_visible_create_attempt_rows(resolver))

    entry_by_id = {entry["id"]: entry for entry in entries}
    assert interrupted_id in entry_by_id
    create_attempt_entry = entry_by_id[interrupted_id]
    assert create_attempt_entry["create_attempt_state"] == "interrupted"
    assert create_attempt_entry["name"] == "Row Test Name"
    assert create_attempt_entry["accent"] == "#a1b2c3"
    assert create_attempt_entry["account"] == "owner@example.com"


def test_landing_page_shows_interrupted_create_attempt_row(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))

    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Row Test Name" in html
    assert "Interrupted" in html
    assert f"/creating/{create_attempt_id}" in html


def test_landing_page_shows_failed_create_attempt_row(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.FAILED, error="mngr create exploded"))

    response = client.get("/")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Create failed" in html
    assert f"/creating/{create_attempt_id}" in html


def test_creating_page_renders_interrupted_record_with_retry_and_discard(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))

    response = client.get(f"/creating/{create_attempt_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "Interrupted" in html
    assert f"/create?retry={create_attempt_id}" in html
    assert "create-attempt-discard-btn" in html


def test_creating_page_renders_failed_record_with_error_and_log_tail(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.FAILED,
            error="clone blew up",
            log_tail=("line one of the tail", "line two of the tail"),
        )
    )

    response = client.get(f"/creating/{create_attempt_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "clone blew up" in html
    assert "line two of the tail" in html
    assert "create-attempt-dismiss-btn" in html


def test_creating_page_redirects_home_without_a_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, _store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.get(f"/creating/{_create_attempt_id()}")

    assert response.status_code == 303
    assert response.headers["Location"] == "/"


def test_create_page_retry_prefills_the_form_from_the_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT, launch_mode=LaunchMode.LIMA))

    response = client.get(f"/create?retry={create_attempt_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "https://example.com/some-repo.git" in html
    assert "feature-branch-7" in html
    assert "Row Test Name" in html


def test_create_page_retry_threads_machine_size_and_drops_a_ghost_cloud_account(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A BYOK retry restores the machine size; a since-deleted account degrades gracefully.

    The record names a cloud account that no longer exists (this test env has
    none configured), so no BYOK option is pre-selected -- but the page still
    renders with the rest of the request pre-filled and the stored machine
    size threaded into the instance-type populate JS (which itself falls back
    to the default when the size is not offered).
    """
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.IN_FLIGHT,
            launch_mode=LaunchMode.GCP,
            provider_instance_name="byok-gcp-ghost",
            cloud_account="byok-gcp-ghost",
            instance_type="e2-standard-4",
        )
    )

    response = client.get(f"/create?retry={create_attempt_id}")

    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "https://example.com/some-repo.git" in html
    assert 'var instanceTypePreselect = "e2-standard-4";' in html
    # The ghost account produced no selected BYOK option (none exists at all).
    assert "BYOK:byok-gcp-ghost" not in html


def test_create_page_ignores_an_unknown_retry_id(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, _store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.get(f"/create?retry={_create_attempt_id()}")

    assert response.status_code == 200
    assert "feature-branch-7" not in response.get_data(as_text=True)


def test_dismiss_create_attempt_deletes_the_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.FAILED, error="boom"))

    response = client.delete(f"/api/v1/workspaces/create-attempts/{create_attempt_id}")

    assert response.status_code == 200
    assert store.read_record(create_attempt_id) is None
    # Idempotent: dismissing again is still a 200 no-op.
    assert client.delete(f"/api/v1/workspaces/create-attempts/{create_attempt_id}").status_code == 200


def test_discard_without_leftover_host_completes_and_deletes_the_record(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    # A non-labeled provider (modal) skips the host lookup entirely.
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))

    post_response = client.post(f"/api/v1/workspaces/create-attempts/{create_attempt_id}/discard")
    assert post_response.status_code == 202
    assert post_response.get_json()["kind"] == "create_attempt_discard"

    # The no-host discard is immediately DONE; the first status read
    # finalizes it (record + discard dir deleted).
    status_response = client.get(f"/api/v1/workspaces/operations/create-attempt-discard/{create_attempt_id}")
    assert status_response.status_code == 200
    assert status_response.get_json()["is_done"] is True
    assert store.read_record(create_attempt_id) is None
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    assert read_discard(create_attempt_id, paths) is None
    # A later poll of the finalized discard reads as unknown.
    assert client.get(f"/api/v1/workspaces/operations/create-attempt-discard/{create_attempt_id}").status_code == 404


def _write_fake_listing_mngr(tmp_path: Path, hosts_payload: dict[str, object]) -> tuple[str, Path]:
    """Fake ``mngr`` for the labeled-provider discard route: ``list`` prints the
    canned hosts payload, ``destroy`` exits 0; every argv lands in a calls log."""
    calls_path = tmp_path / "route-discard-calls.log"
    calls_path.write_text("")
    listing_path = tmp_path / "route-discard-hosts.json"
    listing_path.write_text(json.dumps(hosts_payload))
    script_path = tmp_path / "fake-route-discard-mngr"
    script_path.write_text(
        f'#!/bin/bash\necho "$@" >> "{calls_path}"\nif [ "$1" = "list" ]; then\n  cat "{listing_path}"\nfi\nexit 0\n'
    )
    script_path.chmod(0o755)
    return str(script_path), calls_path


def test_discard_with_leftover_labeled_host_destroys_it_and_finalizes(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """The labeled-provider (lima) path end to end: the route looks the
    leftover host up by its workspace-id label, spawns the detached destroy,
    and the first DONE status read finalizes (record + discard dir gone)."""
    create_attempt_id = _create_attempt_id()
    mngr_binary, calls_path = _write_fake_listing_mngr(
        tmp_path,
        {
            "hosts": [
                {
                    "id": "host-leftover",
                    "name": "row-test-name",
                    "provider": "lima",
                    "state": "BUILDING",
                    "labels": {"workspace-id": create_attempt_id},
                }
            ]
        },
    )
    client, store, _creator = _make_client_with_store(
        tmp_path, root_concurrency_group, notification_dispatcher, mngr_binary=mngr_binary
    )
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.IN_FLIGHT,
            provider_instance_name="lima",
            launch_mode=LaunchMode.LIMA,
        )
    )

    post_response = client.post(f"/api/v1/workspaces/create-attempts/{create_attempt_id}/discard")
    assert post_response.status_code == 202

    # The detached destroy is a real subprocess: wait for it to finish.
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        record = read_discard(create_attempt_id, paths)
        if record is not None and record.status is not CreateAttemptDiscardStatus.RUNNING:
            break
        threading.Event().wait(timeout=0.05)
    else:
        raise AssertionError("discard never reached a terminal status")

    calls = [line for line in calls_path.read_text().splitlines() if line]
    assert "list --hosts --provider lima --format json" in calls
    assert "destroy @host-leftover.lima --force" in calls

    status_response = client.get(f"/api/v1/workspaces/operations/create-attempt-discard/{create_attempt_id}")
    assert status_response.status_code == 200
    assert status_response.get_json()["is_done"] is True
    assert store.read_record(create_attempt_id) is None
    assert read_discard(create_attempt_id, paths) is None


def test_discard_and_dismiss_refuse_a_live_create_attempt_with_409(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A still-running create attempt can be neither discarded nor dismissed: its
    record (and half-built host) belong to the live create."""
    create_attempt_id = _create_attempt_id()
    client, store, _creator = _make_client_with_store(
        tmp_path, root_concurrency_group, notification_dispatcher, live_create_attempt_id_str=create_attempt_id
    )
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))

    discard_response = client.post(f"/api/v1/workspaces/create-attempts/{create_attempt_id}/discard")
    assert discard_response.status_code == 409
    dismiss_response = client.delete(f"/api/v1/workspaces/create-attempts/{create_attempt_id}")
    assert dismiss_response.status_code == 409
    # The record is untouched either way.
    assert store.read_record(create_attempt_id) is not None


def test_discard_of_a_done_record_is_refused_with_409(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A DONE record's workspace exists (its host still carries the
    workspace-id label), so a discard would destroy a healthy workspace; the
    route must refuse and leave the record to the discovery sweep."""
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(
        _record(
            create_attempt_id,
            PendingCreateAttemptState.DONE,
            provider_instance_name="lima",
            launch_mode=LaunchMode.LIMA,
        )
    )

    response = client.post(f"/api/v1/workspaces/create-attempts/{create_attempt_id}/discard")

    assert response.status_code == 409
    assert store.read_record(create_attempt_id) is not None
    # No discard was started for it either.
    assert read_discard(create_attempt_id, WorkspacePaths(data_dir=tmp_path / "minds")) is None


def test_discard_of_unknown_create_attempt_returns_404(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    client, _store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)

    response = client.post(f"/api/v1/workspaces/create-attempts/{_create_attempt_id()}/discard")

    assert response.status_code == 404


def test_discard_status_reports_failed_without_finalizing_when_the_wrapper_died(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    """A non-DONE discard status is reported as-is and finalizes nothing."""
    client, store, _creator = _make_client_with_store(tmp_path, root_concurrency_group, notification_dispatcher)
    create_attempt_id = _create_attempt_id()
    store.write_record(_record(create_attempt_id, PendingCreateAttemptState.IN_FLIGHT))
    # Simulate a discard whose wrapper died without writing an exit code:
    # derived status FAILED, never finalized, record kept.
    paths = WorkspacePaths(data_dir=tmp_path / "minds")
    discard_dir = paths.data_dir / "discarding_create_attempts" / create_attempt_id
    discard_dir.mkdir(parents=True)
    (discard_dir / "output.log").write_text("partial output\n")
    (discard_dir / "pid").write_text("999999999\n")

    status_response = client.get(f"/api/v1/workspaces/operations/create-attempt-discard/{create_attempt_id}")

    assert status_response.status_code == 200
    payload = status_response.get_json()
    assert payload["status"] == str(CreateAttemptDiscardStatus.FAILED)
    assert payload["is_done"] is False
    assert store.read_record(create_attempt_id) is not None


class _FixedLogSinkAgentCreator(AgentCreator):
    """Creator serving one pre-filled log sink, for the SSE replay test."""

    fixed_create_attempt_id_str: str
    fixed_log_sink: CreateAttemptLogSink

    def get_log_sink(self, create_attempt_id: CreateAttemptId) -> CreateAttemptLogSink | None:
        return self.fixed_log_sink if str(create_attempt_id) == self.fixed_create_attempt_id_str else None


def test_create_operation_log_stream_replays_history_for_every_reader(
    tmp_path: Path,
    root_concurrency_group: ConcurrencyGroup,
    notification_dispatcher: NotificationDispatcher,
) -> None:
    create_attempt_id = CreateAttemptId()
    log_sink = CreateAttemptLogSink()
    log_sink.put("first history line")
    log_sink.put("second history line")
    log_sink.put(LOG_SENTINEL)
    creator = _FixedLogSinkAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
        notification_dispatcher=notification_dispatcher,
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_create_attempt_id_str=str(create_attempt_id),
        fixed_log_sink=log_sink,
    )
    auth_store = FileAuthStore(data_directory=tmp_path / "auth")
    app = create_desktop_client(
        auth_store=auth_store,
        backend_resolver=StaticBackendResolver(url_by_agent_and_service={}),
        http_client=None,
        agent_creator=creator,
        paths=WorkspacePaths(data_dir=tmp_path / "minds"),
        root_concurrency_group=root_concurrency_group,
    )
    client = app.test_client()
    client.set_cookie(SESSION_COOKIE_NAME, create_session_cookie(signing_key=auth_store.get_signing_key()))

    # Two sequential readers each replay the full history (the buffer is not
    # consume-once) and both see the terminal done frame.
    for _reader in range(2):
        response = client.get(f"/api/v1/workspaces/operations/create/{create_attempt_id}/logs")
        assert response.status_code == 200
        body = response.get_data(as_text=True)
        assert '"first history line"' in body
        assert '"second history line"' in body
        assert '"done": true' in body
