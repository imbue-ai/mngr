import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from imbue.minds.desktop_client.pending_create_attempts import FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.primitives import LaunchMode


def _make_record(
    create_attempt_id: str, state: PendingCreateAttemptState = PendingCreateAttemptState.IN_FLIGHT
) -> PendingCreateAttemptRecord:
    now = datetime.now(timezone.utc)
    return PendingCreateAttemptRecord(
        create_attempt_id=create_attempt_id,
        state=state,
        provider_instance_name="lima",
        created_at=now,
        updated_at=now,
        request=PendingCreateAttemptRequest(
            repo_source="https://example.com/repo.git",
            host_name="workspace-1",
            display_name="Machine 1",
            launch_mode=LaunchMode.LIMA,
            account_id="user-1",
            account_email="user-1@example.com",
            color="#a1b2c3",
        ),
    )


def test_pending_create_attempt_record_round_trips_through_the_store(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    record = _make_record(create_attempt_id)

    store.write_record(record)
    read_back = store.read_record(create_attempt_id)

    assert read_back == record
    assert store.list_records() == [record]


def test_read_record_returns_none_for_missing_record(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    assert store.read_record("create-attempt-does-not-exist") is None


def test_read_record_tolerates_unknown_fields_from_newer_versions(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))

    # Simulate a record written by a newer build carrying an extra field.
    path = tmp_path / "pending" / f"{create_attempt_id}.json"
    raw = json.loads(path.read_text())
    raw["field_from_the_future"] = {"nested": True}
    raw["request"]["another_future_field"] = "x"
    path.write_text(json.dumps(raw))

    read_back = store.read_record(create_attempt_id)
    assert read_back is not None
    assert read_back.create_attempt_id == create_attempt_id


def test_list_records_skips_malformed_files(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))
    (tmp_path / "pending" / "create-attempt-garbage.json").write_text("{not json")

    records = store.list_records()
    assert [record.create_attempt_id for record in records] == [create_attempt_id]


def test_mark_failed_snapshots_error_and_truncates_log_tail(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))

    long_tail = tuple(f"line-{i}" for i in range(FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES + 250))
    store.mark_failed(create_attempt_id, error="boom", error_kind="GIT_AUTH_REQUIRED", log_tail=long_tail)

    record = store.read_record(create_attempt_id)
    assert record is not None
    assert record.state is PendingCreateAttemptState.FAILED
    assert record.error == "boom"
    assert record.error_kind == "GIT_AUTH_REQUIRED"
    # The stored tail keeps only the LAST max-lines entries.
    assert len(record.log_tail) == FAILED_CREATE_ATTEMPT_LOG_TAIL_MAX_LINES
    assert record.log_tail[-1] == long_tail[-1]
    assert record.log_tail[0] == long_tail[250]


def test_mark_done_sets_canonical_ids_and_flags_awaiting_confirmation(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))
    assert store.has_done_records() is False

    store.mark_done(create_attempt_id, agent_id="agent-abc", host_id="host-def")

    record = store.read_record(create_attempt_id)
    assert record is not None
    assert record.state is PendingCreateAttemptState.DONE
    assert record.agent_id == "agent-abc"
    assert record.host_id == "host-def"
    assert store.has_done_records() is True


def test_sweep_confirmed_records_deletes_only_discovery_confirmed_create_attempts(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    confirmed_id = f"create-attempt-{uuid4().hex}"
    unconfirmed_id = f"create-attempt-{uuid4().hex}"
    in_flight_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(confirmed_id))
    store.write_record(_make_record(unconfirmed_id))
    store.write_record(_make_record(in_flight_id))
    store.mark_done(confirmed_id, agent_id="agent-confirmed", host_id="host-1")
    store.mark_done(unconfirmed_id, agent_id="agent-unconfirmed", host_id="host-2")

    deleted = store.sweep_confirmed_records(frozenset({"agent-confirmed", "agent-unrelated"}))

    assert deleted == [confirmed_id]
    assert store.read_record(confirmed_id) is None
    # DONE-but-unconfirmed and IN_FLIGHT records survive (no timeout bound).
    assert store.read_record(unconfirmed_id) is not None
    assert store.read_record(in_flight_id) is not None
    assert store.has_done_records() is True


def test_done_records_are_reloaded_across_store_instances(tmp_path: Path) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    first_store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    first_store.write_record(_make_record(create_attempt_id))
    first_store.mark_done(create_attempt_id, agent_id="agent-xyz", host_id="host-xyz")

    # A fresh store (a new app session) still knows a DONE record awaits
    # confirmation, so the discovery sweep keeps working across restarts.
    second_store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    assert second_store.has_done_records() is True
    deleted = second_store.sweep_confirmed_records(frozenset({"agent-xyz"}))
    assert deleted == [create_attempt_id]
    assert second_store.has_done_records() is False


def test_failed_delete_keeps_done_record_in_the_sweep_set(tmp_path: Path) -> None:
    """A transient unlink failure must not lose track of a DONE record.

    The sweep set is what makes the discovery sweep retry the deletion on the
    next resolver change; dropping the id on a failed unlink would strand the
    record until the next restart re-seeds the set from disk.
    """
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))
    store.mark_done(create_attempt_id, agent_id="agent-abc", host_id="host-def")

    # Replace the record file with a directory so ``unlink`` deterministically
    # raises an OSError (IsADirectoryError on Linux, PermissionError on macOS)
    # regardless of the user the tests run as.
    path = tmp_path / "pending" / f"{create_attempt_id}.json"
    path.unlink()
    path.mkdir()

    assert store.delete_record(create_attempt_id) is False
    # The record still awaits confirmation, so the sweep will retry.
    assert store.has_done_records() is True


def test_delete_record_is_idempotent(tmp_path: Path) -> None:
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    store.write_record(_make_record(create_attempt_id))

    assert store.delete_record(create_attempt_id) is True
    assert store.delete_record(create_attempt_id) is False
    assert store.read_record(create_attempt_id) is None
