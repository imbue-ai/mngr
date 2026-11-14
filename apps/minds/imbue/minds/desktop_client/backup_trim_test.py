from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest
from pydantic import AnyUrl

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_trim import BackupTrimManager
from imbue.minds.desktop_client.backup_trim import BackupTrimState
from imbue.minds.desktop_client.backup_trim import BackupTrimStatus
from imbue.minds.desktop_client.backup_trim import collect_trimmable_repos
from imbue.minds.desktop_client.backup_trim import max_repack_bytes_for
from imbue.minds.desktop_client.backup_trim import run_backup_trim
from imbue.minds.desktop_client.backup_trim import select_snapshot_ids_to_forget
from imbue.minds.desktop_client.backup_trim import user_segment_for
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryInfo
from imbue.minds.desktop_client.imbue_cloud_cli import BackupStorageUsage
from imbue.minds.desktop_client.notification_feed import NotificationFeed
from imbue.minds.desktop_client.restic_cli import ResticSnapshot
from imbue.minds.desktop_client.restic_cli import forget_snapshots
from imbue.minds.desktop_client.restic_cli import list_snapshots
from imbue.minds.desktop_client.restic_cli import prune_repository
from imbue.minds.desktop_client.testing import FAKE_BACKUP_USER_ID
from imbue.minds.desktop_client.testing import FAKE_BACKUP_USER_SEGMENT
from imbue.minds.desktop_client.testing import legacy_restic_env
from imbue.minds.desktop_client.testing import make_notification_feed_for_test
from imbue.minds.desktop_client.testing import store_repository_url
from imbue.minds.desktop_client.testing import store_restic_env
from imbue.minds.desktop_client.ui_models import NotificationKind
from imbue.minds.errors import BackupProvisioningError
from imbue.mngr.primitives import AgentId
from imbue.mngr.utils.polling import poll_for_value

_WORKSPACE_ID = "agent-" + "a" * 32
_OTHER_WORKSPACE_ID = "agent-" + "b" * 32
_STORE_REPOSITORY = store_repository_url(_WORKSPACE_ID)
_GIB = 1024**3


def _snapshot(snapshot_id: str, hour: int) -> ResticSnapshot:
    return ResticSnapshot(
        snapshot_id=snapshot_id,
        short_id=snapshot_id[:8],
        time=datetime(2026, 7, 20, hour, 0, 0, tzinfo=timezone.utc),
    )


def _usage(used: int, quota: int = 100) -> BackupStorageUsage:
    return BackupStorageUsage(used_bytes=used, quota_bytes=quota, hard_limit_bytes=quota + _GIB, mode="active")


def _repository_info(workspace_id: str) -> BackupRepositoryInfo:
    return BackupRepositoryInfo(
        workspace_id=workspace_id,
        repository_url=store_repository_url(workspace_id),
        used_bytes=0,
        created_at="2026-01-01T00:00:00+00:00",
    )


def test_user_segment_strips_hyphens() -> None:
    assert user_segment_for(FAKE_BACKUP_USER_ID) == FAKE_BACKUP_USER_SEGMENT


def test_max_repack_bytes_is_half_the_headroom() -> None:
    assert max_repack_bytes_for(_usage(10, 100)) == _GIB // 2
    assert (
        max_repack_bytes_for(BackupStorageUsage(used_bytes=1, quota_bytes=5, hard_limit_bytes=5, mode="active"))
        is None
    )


def test_select_snapshot_ids_to_forget_takes_oldest_half_never_latest() -> None:
    assert select_snapshot_ids_to_forget([]) == []
    assert select_snapshot_ids_to_forget([_snapshot("only", 1)]) == []
    assert select_snapshot_ids_to_forget([_snapshot("new", 2), _snapshot("old", 1)]) == ["old"]
    five = [_snapshot(f"s{i}", i) for i in range(5)]
    assert select_snapshot_ids_to_forget(list(reversed(five))) == ["s0", "s1"]


def test_collect_trimmable_repos_parses_canonical_envs(tmp_path: Path) -> None:
    paths = InstallationPaths(data_dir=tmp_path)
    write_canonical_env(
        paths,
        AgentId(_WORKSPACE_ID),
        f"RESTIC_REPOSITORY={_STORE_REPOSITORY}\n"
        "RESTIC_PASSWORD=secret\n"
        f"RESTIC_REST_USERNAME={_WORKSPACE_ID}\n"
        "RESTIC_REST_PASSWORD=tok\n",
    )
    # Another account's store repository, a legacy bucket, and a BYO backend are not trimmable here.
    write_canonical_env(
        paths,
        AgentId(_OTHER_WORKSPACE_ID),
        store_restic_env(_OTHER_WORKSPACE_ID, user_segment="f" * 32),
    )
    write_canonical_env(
        paths,
        AgentId(),
        legacy_restic_env(f"{FAKE_BACKUP_USER_SEGMENT[:16]}--host-abc"),
    )
    # Another account's legacy bucket is not this account's leftover.
    write_canonical_env(
        paths,
        AgentId(),
        "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/ffffffffffffffff--host-def\nRESTIC_PASSWORD=pw\n",
    )
    write_canonical_env(paths, AgentId(), "RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/mine\nRESTIC_PASSWORD=pw\n")
    collection = collect_trimmable_repos(paths, user_segment=FAKE_BACKUP_USER_SEGMENT)
    assert set(collection.repo_by_workspace_id) == {_WORKSPACE_ID}
    repo = collection.repo_by_workspace_id[_WORKSPACE_ID]
    assert repo.password == "secret"
    assert repo.backend_env == {"RESTIC_REST_USERNAME": _WORKSPACE_ID, "RESTIC_REST_PASSWORD": "tok"}
    assert collection.legacy_repository_count == 1
    # A machine with no env dir has no trimmable repos.
    empty = collect_trimmable_repos(
        InstallationPaths(data_dir=tmp_path / "empty"), user_segment=FAKE_BACKUP_USER_SEGMENT
    )
    assert empty.repo_by_workspace_id == {} and empty.legacy_repository_count == 0


def _run_trim(
    cli: FakeImbueCloudCli,
    paths: InstallationPaths,
    *,
    report_progress: Callable[[str], None] = lambda _detail: None,
    list_snapshots_fn: Callable[..., tuple[ResticSnapshot, ...]] = list_snapshots,
    forget_snapshots_fn: Callable[..., None] = forget_snapshots,
    prune_fn: Callable[..., None] = prune_repository,
) -> tuple[bool, str]:
    return run_backup_trim(
        account_email="a@example.com",
        user_id=FAKE_BACKUP_USER_ID,
        cli=cli,
        paths=paths,
        report_progress=report_progress,
        list_snapshots_fn=list_snapshots_fn,
        forget_snapshots_fn=forget_snapshots_fn,
        prune_fn=prune_fn,
    )


def test_run_backup_trim_short_circuits_when_already_under_quota(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.backup_usage_results = [_usage(10)]
    is_under, detail = _run_trim(cli, InstallationPaths(data_dir=tmp_path))
    assert is_under is True
    assert "already under" in detail


def test_run_backup_trim_forgets_oldest_half_prunes_within_headroom_and_finishes_when_under(tmp_path: Path) -> None:
    paths = InstallationPaths(data_dir=tmp_path)
    write_canonical_env(paths, AgentId(_WORKSPACE_ID), store_restic_env(_WORKSPACE_ID))
    cli = make_fake_imbue_cloud_cli()
    # Over quota initially; under after the first round.
    cli.backup_usage_results = [_usage(1000), _usage(40)]
    cli.backup_repositories = [_repository_info(_WORKSPACE_ID), _repository_info(_OTHER_WORKSPACE_ID)]
    forgotten: list[tuple[str, tuple[str, ...]]] = []
    pruned: list[tuple[str, int | None]] = []

    def _fake_list_snapshots(
        *, repository: str, backend_env: Mapping[str, str], password: str | None
    ) -> tuple[ResticSnapshot, ...]:
        return (_snapshot("older", 1), _snapshot("newest", 3), _snapshot("oldest", 0))

    def _fake_forget(
        *,
        repository: str,
        backend_env: Mapping[str, str],
        password: str | None,
        snapshot_ids: list[str],
    ) -> None:
        forgotten.append((repository, tuple(snapshot_ids)))

    def _fake_prune(
        *, repository: str, backend_env: Mapping[str, str], password: str | None, max_repack_bytes: int | None
    ) -> None:
        pruned.append((repository, max_repack_bytes))

    progress_lines: list[str] = []
    is_under, detail = _run_trim(
        cli,
        paths,
        report_progress=progress_lines.append,
        list_snapshots_fn=_fake_list_snapshots,
        forget_snapshots_fn=_fake_forget,
        prune_fn=_fake_prune,
    )
    assert is_under is True
    # The oldest snapshot (of three) is forgotten, then one prune reclaims
    # the space with a repack cap of half the headroom.
    assert forgotten == [(_STORE_REPOSITORY, ("oldest",))]
    assert pruned == [(_STORE_REPOSITORY, _GIB // 2)]
    assert any("removing 1 of 3" in line for line in progress_lines)
    assert "new backups will resume" in detail


def test_run_backup_trim_skips_a_local_env_the_store_no_longer_lists(tmp_path: Path) -> None:
    # A repository reaped elsewhere (or re-keyed by another device) still has a
    # local env here; opening it would fail, and must not abort the trim of
    # the repositories the store does list.
    paths = InstallationPaths(data_dir=tmp_path)
    write_canonical_env(paths, AgentId(_WORKSPACE_ID), store_restic_env(_WORKSPACE_ID))
    gone_repository = store_repository_url(_OTHER_WORKSPACE_ID)
    write_canonical_env(
        paths, AgentId(_OTHER_WORKSPACE_ID), f"RESTIC_REPOSITORY={gone_repository}\nRESTIC_PASSWORD=pw\n"
    )
    cli = make_fake_imbue_cloud_cli()
    cli.backup_usage_results = [_usage(1000), _usage(40)]
    cli.backup_repositories = [_repository_info(_WORKSPACE_ID)]
    listed: list[str] = []

    def _fake_list_snapshots(
        *, repository: str, backend_env: Mapping[str, str], password: str | None
    ) -> tuple[ResticSnapshot, ...]:
        if repository == gone_repository:
            raise BackupProvisioningError("unexpected HTTP response (401)")
        listed.append(repository)
        return (_snapshot("older", 1), _snapshot("newest", 3))

    is_under, _detail = _run_trim(
        cli,
        paths,
        list_snapshots_fn=_fake_list_snapshots,
        forget_snapshots_fn=lambda **kwargs: None,
        prune_fn=lambda **kwargs: None,
    )

    assert is_under is True
    assert listed == [_STORE_REPOSITORY]


def test_run_backup_trim_reports_untrimmable_and_legacy_repositories_when_still_over(tmp_path: Path) -> None:
    """With no local env for the store repository and one legacy env, nothing can be forgotten and the run says why."""
    paths = InstallationPaths(data_dir=tmp_path)
    write_canonical_env(
        paths,
        AgentId(),
        legacy_restic_env(f"{FAKE_BACKUP_USER_SEGMENT[:16]}--host-abc"),
    )
    cli = make_fake_imbue_cloud_cli()
    cli.backup_usage_results = [_usage(1000)]
    cli.backup_repositories = [_repository_info(_OTHER_WORKSPACE_ID)]
    is_under, detail = _run_trim(cli, paths)
    assert is_under is False
    assert _OTHER_WORKSPACE_ID in detail
    assert "1 backup(s) are still on legacy storage" in detail


class _CrashingImbueCloudCli(FakeImbueCloudCli):
    """Fake whose usage probe raises an exception type the trim run does not expect."""

    def get_backup_usage(self, account: str) -> BackupStorageUsage:
        raise RuntimeError("boom")


def _wait_until_not_running(manager: BackupTrimManager, user_id: str) -> BackupTrimStatus:
    def _finished_status() -> BackupTrimStatus | None:
        status = manager.get_status(user_id)
        if status is not None and not status.is_running:
            return status
        return None

    finished, _poll_count, _elapsed = poll_for_value(_finished_status, timeout=10.0, poll_interval=0.01)
    assert finished is not None, "trim run did not finish within the deadline"
    return finished


def _wait_for_system_events(feed: NotificationFeed) -> list[tuple[str, str, str]]:
    """The trim thread flips the status to its terminal state BEFORE it notifies, so a run
    that has stopped running has not necessarily appended its event yet. Returns
    (title, body, workspace_agent_id) per system event in the feed."""

    def _events() -> list[tuple[str, str, str]] | None:
        events = [
            (entry.title, entry.body, entry.workspace_agent_id)
            for entry in feed.reconcile((), {}).entries
            if entry.kind == NotificationKind.SYSTEM_EVENT
        ]
        return events or None

    events, _poll_count, _elapsed = poll_for_value(_events, timeout=10.0, poll_interval=0.01)
    assert events is not None, "the finished trim run never appended its system event"
    return events


def test_backup_trim_manager_start_trim_records_success_and_notifies(tmp_path: Path) -> None:
    manager = BackupTrimManager()
    feed = make_notification_feed_for_test(dispatcher=None, is_main_window_focused=False)
    cli = make_fake_imbue_cloud_cli()
    cli.backup_usage_results = [_usage(10)]
    started = manager.start_trim(
        user_id="user-1",
        account_email="a@example.com",
        cli=cli,
        paths=InstallationPaths(data_dir=tmp_path),
        notification_feed=feed,
    )
    assert started is True
    outcome = _wait_until_not_running(manager, "user-1")
    assert outcome.state == BackupTrimState.SUCCEEDED
    # Account-level: no workspace, so the entry carries none and names the account.
    assert _wait_for_system_events(feed) == [("Backup cleanup finished", outcome.detail, "")]
    (entry,) = feed.reconcile((), {}).entries
    assert (entry.workspace_name, entry.account_id) == ("a@example.com", "user-1")


def test_backup_trim_manager_refuses_second_start_while_running(tmp_path: Path) -> None:
    manager = BackupTrimManager()
    manager.status_by_user_id["user-1"] = BackupTrimStatus(
        state=BackupTrimState.RUNNING, detail="in flight", error_detail=""
    )
    started = manager.start_trim(
        user_id="user-1",
        account_email="a@example.com",
        cli=make_fake_imbue_cloud_cli(),
        paths=InstallationPaths(data_dir=tmp_path),
        notification_feed=None,
    )
    assert started is False
    status = manager.get_status("user-1")
    assert status is not None
    assert status.detail == "in flight"


def test_backup_trim_manager_records_failure_and_notifies(tmp_path: Path) -> None:
    """A typed CLI failure lands as a failed status plus the failure notification."""
    manager = BackupTrimManager()
    feed = make_notification_feed_for_test(dispatcher=None, is_main_window_focused=False)
    # No fake usage results configured -> the CLI raises ImbueCloudCliError.
    manager._run(
        user_id="user-1",
        account_email="a@example.com",
        cli=make_fake_imbue_cloud_cli(),
        paths=InstallationPaths(data_dir=tmp_path),
        notification_feed=feed,
    )
    status = manager.get_status("user-1")
    assert status is not None
    assert status.state == BackupTrimState.FAILED
    assert status.detail == "Backup cleanup didn't finish. Try again. If this persists, report a bug."
    assert status.error_detail != ""
    assert _wait_for_system_events(feed) == [("Backup cleanup failed", status.detail, "")]


def test_backup_trim_manager_flips_unexpected_crash_to_failed(tmp_path: Path) -> None:
    """An exception type the run does not expect must not leave the status stuck on running."""
    manager = BackupTrimManager()
    manager.status_by_user_id["user-1"] = BackupTrimStatus(
        state=BackupTrimState.RUNNING, detail="starting", error_detail=""
    )
    feed = make_notification_feed_for_test(dispatcher=None, is_main_window_focused=False)
    with pytest.raises(RuntimeError, match="boom"):
        manager._run(
            user_id="user-1",
            account_email="a@example.com",
            cli=_CrashingImbueCloudCli(connector_url=AnyUrl("http://connector.invalid")),
            paths=InstallationPaths(data_dir=tmp_path),
            notification_feed=feed,
        )
    status = manager.get_status("user-1")
    assert status is not None
    assert status.state == BackupTrimState.FAILED
    assert status.detail == "Backup cleanup didn't finish. Try again. If this persists, report a bug."
    assert "stopped unexpectedly" in status.error_detail
    assert _wait_for_system_events(feed) == [("Backup cleanup failed", status.detail, "")]
