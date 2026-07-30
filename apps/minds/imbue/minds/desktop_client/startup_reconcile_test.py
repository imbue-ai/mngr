"""Unit tests for the startup Lima/Docker host reconcile.

The reconciler talks to mngr exclusively through the CLI, so these tests run
it against a fake ``mngr`` executable that serves canned ``list --hosts``
JSON and records every invocation -- the assertions are on exactly which
mngr commands the policy issued (destroy / gc) and on the store / session
side effects (adoption, record transitions).
"""

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from uuid import uuid4

from pydantic import Field

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.agent_creator import AgentCreator
from imbue.minds.desktop_client.backend_resolver import MngrCliBackendResolver
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.conftest import make_session_store_for_test
from imbue.minds.desktop_client.notification import NotificationDispatcher
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRecord
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptRequest
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptState
from imbue.minds.desktop_client.pending_create_attempts import PendingCreateAttemptStore
from imbue.minds.desktop_client.session_store import MultiAccountSessionStore
from imbue.minds.desktop_client.startup_reconcile import StartupHostReconciler
from imbue.minds.desktop_client.system_interface_health import SystemInterfaceHealthTracker
from imbue.minds.primitives import LaunchMode


def _write_fake_mngr(tmp_path: Path, hosts_by_provider: dict[str, list[dict[str, object]]]) -> tuple[str, Path]:
    """Create a fake ``mngr`` executable serving canned host listings.

    Every invocation's argv is appended to a calls log (one line per call);
    ``list --hosts --provider X`` prints the canned listing for provider X.
    Returns ``(binary_path_str, calls_log_path)``.
    """
    calls_path = tmp_path / "mngr-calls.log"
    calls_path.write_text("")
    listings_dir = tmp_path / "listings"
    listings_dir.mkdir(exist_ok=True)
    for provider_name, hosts in hosts_by_provider.items():
        (listings_dir / f"{provider_name}.json").write_text(json.dumps({"hosts": hosts}))
    script_path = tmp_path / "fake-mngr"
    script_path.write_text(
        "#!/bin/bash\n"
        "set -euo pipefail\n"
        f'echo "$@" >> "{calls_path}"\n'
        'if [ "$1" = "list" ]; then\n'
        '  provider=""\n'
        '  prev=""\n'
        '  for arg in "$@"; do\n'
        '    if [ "$prev" = "--provider" ]; then provider="$arg"; fi\n'
        '    prev="$arg"\n'
        "  done\n"
        f'  cat "{listings_dir}/$provider.json"\n'
        "fi\n"
        "exit 0\n"
    )
    script_path.chmod(0o755)
    return str(script_path), calls_path


def _read_calls(calls_path: Path) -> list[str]:
    return [line for line in calls_path.read_text().splitlines() if line]


def _make_record(
    create_attempt_id: str,
    *,
    account_id: str = "",
    created_at: datetime | None = None,
) -> PendingCreateAttemptRecord:
    effective_created_at = created_at if created_at is not None else datetime.now(timezone.utc)
    return PendingCreateAttemptRecord(
        create_attempt_id=create_attempt_id,
        state=PendingCreateAttemptState.IN_FLIGHT,
        provider_instance_name="lima",
        created_at=effective_created_at,
        updated_at=effective_created_at,
        request=PendingCreateAttemptRequest(
            repo_source="https://example.com/repo.git",
            host_name="foo",
            display_name="Foo Machine",
            launch_mode=LaunchMode.LIMA,
            account_id=account_id,
            account_email=f"{account_id}@example.com" if account_id else "",
            color="#a1b2c3",
        ),
    )


class _LiveIdsAgentCreator(AgentCreator):
    """AgentCreator test double reporting a fixed set of live create attempt ids."""

    fixed_live_create_attempt_ids: tuple[str, ...] = Field(default=(), frozen=True)

    def live_in_flight_create_attempt_ids(self) -> set[str]:
        return set(self.fixed_live_create_attempt_ids)


def _make_reconciler(
    tmp_path: Path,
    concurrency_group: ConcurrencyGroup,
    mngr_binary: str,
    store: PendingCreateAttemptStore,
    *,
    session_store: MultiAccountSessionStore | None = None,
    live_create_attempt_ids: tuple[str, ...] = (),
) -> StartupHostReconciler:
    creator = _LiveIdsAgentCreator(
        paths=WorkspacePaths(data_dir=tmp_path / "minds-data"),
        root_concurrency_group=concurrency_group,
        notification_dispatcher=NotificationDispatcher.create(is_electron=False, tkinter_module=None, is_macos=False),
        system_interface_health_tracker=SystemInterfaceHealthTracker(),
        fixed_live_create_attempt_ids=live_create_attempt_ids,
    )
    return StartupHostReconciler(
        backend_resolver=MngrCliBackendResolver(),
        agent_creator=creator,
        pending_create_attempt_store=store,
        session_store=session_store,
        mngr_binary=mngr_binary,
        mngr_host_dir=tmp_path / "mngr",
        concurrency_group=concurrency_group,
    )


def test_reconcile_destroys_labeled_half_built_host_past_grace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                }
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    stale_created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    store.write_record(_make_record(create_attempt_id, created_at=stale_created_at))

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    calls = _read_calls(calls_path)
    assert f"destroy @{host_id}.lima --force" in calls
    # The interrupted record persists (it backs the interrupted row's retry/dismiss).
    record = store.read_record(create_attempt_id)
    assert record is not None
    assert record.state is PendingCreateAttemptState.IN_FLIGHT


def test_reconcile_leaves_half_built_host_within_grace(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                }
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    store.write_record(_make_record(create_attempt_id))

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))


def test_reconcile_destroys_labeled_half_built_host_with_no_record(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [],
            "docker": [
                {
                    "id": host_id,
                    "name": "bar",
                    "provider": "docker",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                }
            ],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    assert f"destroy @{host_id}.docker --force" in _read_calls(calls_path)


def test_reconcile_never_touches_unlabeled_hosts(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "pre-existing-orphan",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {},
                    "agents": [],
                }
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))


def test_reconcile_skips_hosts_of_live_in_flight_create_attempts(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                }
            ],
            "docker": [],
        },
    )
    # No pending record at all -- without the live guard this host would be
    # destroyed immediately; the live create attempt id must protect it.
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(
        tmp_path, root_concurrency_group, mngr_binary, store, live_create_attempt_ids=(create_attempt_id,)
    )
    reconciler.reconcile_now()

    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))


def test_reconcile_skips_failed_and_destroyed_host_records(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": f"host-{uuid4().hex}",
                    "name": "failed-host",
                    "provider": "lima",
                    "state": "FAILED",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                },
                {
                    "id": f"host-{uuid4().hex}",
                    "name": "destroyed-host",
                    "provider": "lima",
                    "state": "DESTROYED",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [],
                },
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))


def test_reconcile_adopts_completed_host_and_restores_association(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    services_agent_id = f"agent-{uuid4().hex}"
    account_id = f"user-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [{"id": services_agent_id, "name": "system-services"}],
                }
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")
    stale_created_at = datetime.now(timezone.utc) - timedelta(hours=2)
    store.write_record(_make_record(create_attempt_id, account_id=account_id, created_at=stale_created_at))

    fake_cli = make_fake_imbue_cloud_cli()
    fake_cli.add_account(user_id=account_id, email=f"{account_id}@example.com")
    session_store = make_session_store_for_test(tmp_path / "sessions", cli=fake_cli)

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store, session_store=session_store)
    reconciler.reconcile_now()

    # The finished workspace is never destroyed...
    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))
    # ...its account association is restored...
    account = session_store.get_account_for_workspace(services_agent_id)
    assert account is not None
    assert account.user_id == account_id
    # ...and the record now awaits discovery confirmation like any other
    # successful create attempt.
    record = store.read_record(create_attempt_id)
    assert record is not None
    assert record.state is PendingCreateAttemptState.DONE
    assert record.agent_id == services_agent_id
    assert record.host_id == host_id


def test_reconcile_adopts_completed_host_without_record_as_private(
    tmp_path: Path, root_concurrency_group: ConcurrencyGroup
) -> None:
    create_attempt_id = f"create-attempt-{uuid4().hex}"
    host_id = f"host-{uuid4().hex}"
    mngr_binary, calls_path = _write_fake_mngr(
        tmp_path,
        {
            "lima": [
                {
                    "id": host_id,
                    "name": "foo",
                    "provider": "lima",
                    "state": "RUNNING",
                    "labels": {"workspace-id": create_attempt_id},
                    "agents": [{"id": f"agent-{uuid4().hex}", "name": "system-services"}],
                }
            ],
            "docker": [],
        },
    )
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    # Nothing is destroyed and nothing crashes: the workspace stays private.
    assert not any(call.startswith("destroy") for call in _read_calls(calls_path))


def test_reconcile_runs_gc_scoped_to_lima_and_docker(tmp_path: Path, root_concurrency_group: ConcurrencyGroup) -> None:
    mngr_binary, calls_path = _write_fake_mngr(tmp_path, {"lima": [], "docker": []})
    store = PendingCreateAttemptStore(records_dir=tmp_path / "pending")

    reconciler = _make_reconciler(tmp_path, root_concurrency_group, mngr_binary, store)
    reconciler.reconcile_now()

    calls = _read_calls(calls_path)
    gc_calls = [call for call in calls if call.startswith("gc ")]
    assert gc_calls == ["gc --on-error continue --format json --provider lima --provider docker"]
    # Both providers were listed (host-inventory reads).
    assert "list --hosts --provider lima --format json" in calls
    assert "list --hosts --provider docker --format json" in calls
