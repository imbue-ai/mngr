from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_trim import bucket_name_from_repository
from imbue.minds.desktop_client.backup_trim import collect_trimmable_repos
from imbue.minds.desktop_client.backup_trim import run_backup_trim
from imbue.minds.desktop_client.backup_trim import select_snapshot_ids_to_forget
from imbue.minds.desktop_client.conftest import make_fake_imbue_cloud_cli
from imbue.minds.desktop_client.restic_cli import ResticSnapshot
from imbue.mngr.primitives import AgentId


def _snapshot(snapshot_id: str, hour: int) -> ResticSnapshot:
    return ResticSnapshot(
        snapshot_id=snapshot_id,
        short_id=snapshot_id[:8],
        time=datetime(2026, 7, 20, hour, 0, 0, tzinfo=timezone.utc),
    )


def test_bucket_name_from_repository_parses_s3_urls() -> None:
    assert bucket_name_from_repository("s3:https://acct.r2.cloudflarestorage.com/u1--host-abc") == "u1--host-abc"
    assert bucket_name_from_repository("s3:acct.r2.cloudflarestorage.com/u1--host-abc") == "u1--host-abc"
    assert bucket_name_from_repository("s3:https://endpoint.example.com/bucket/sub/path") == "bucket"
    assert bucket_name_from_repository("rest:https://example.com/repo") is None
    assert bucket_name_from_repository("s3:https://endpoint-only.example.com") is None


def test_select_snapshot_ids_to_forget_takes_oldest_half_never_latest() -> None:
    assert select_snapshot_ids_to_forget([]) == []
    assert select_snapshot_ids_to_forget([_snapshot("only", 1)]) == []
    assert select_snapshot_ids_to_forget([_snapshot("new", 2), _snapshot("old", 1)]) == ["old"]
    five = [_snapshot(f"s{i}", i) for i in range(5)]
    assert select_snapshot_ids_to_forget(list(reversed(five))) == ["s0", "s1"]


def test_collect_trimmable_repos_parses_canonical_envs(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    write_canonical_env(
        paths,
        agent_id,
        "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/u1--host-abc\n"
        "RESTIC_PASSWORD=secret\n"
        "AWS_ACCESS_KEY_ID=akid\n"
        "AWS_SECRET_ACCESS_KEY=sk\n",
    )
    repos = collect_trimmable_repos(paths)
    assert set(repos) == {"u1--host-abc"}
    repo = repos["u1--host-abc"]
    assert repo.password == "secret"
    assert repo.backend_env == {"AWS_ACCESS_KEY_ID": "akid", "AWS_SECRET_ACCESS_KEY": "sk"}
    # A machine with no env dir has no trimmable repos.
    assert collect_trimmable_repos(WorkspacePaths(data_dir=tmp_path / "empty")) == {}


def test_run_backup_trim_short_circuits_when_already_under_quota(tmp_path: Path) -> None:
    cli = make_fake_imbue_cloud_cli()
    cli.storage_recheck_results = [{"is_over_quota": False, "usage_bytes": 10, "limit_bytes": 100}]
    is_under, detail = run_backup_trim(
        account_email="a@example.com",
        cli=cli,
        paths=WorkspacePaths(data_dir=tmp_path),
        report_progress=lambda _detail: None,
    )
    assert is_under is True
    assert cli.cleanup_grant_call_count == 0
    assert "already under" in detail


def test_run_backup_trim_forgets_oldest_half_and_finishes_when_under(tmp_path: Path) -> None:
    paths = WorkspacePaths(data_dir=tmp_path)
    agent_id = AgentId()
    write_canonical_env(
        paths,
        agent_id,
        "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/u1--host-abc\nRESTIC_PASSWORD=pw\n",
    )
    cli = make_fake_imbue_cloud_cli()
    # Over quota initially and after round 1's grant; under after the forget.
    cli.storage_recheck_results = [
        {"is_over_quota": True, "usage_bytes": 1000, "limit_bytes": 100},
        {"is_over_quota": False, "usage_bytes": 40, "limit_bytes": 100},
    ]
    cli.cleanup_grant_result = {
        "status": "granted",
        "keys": [
            {"bucket_name": "u1--host-abc", "access_key_id": "k1"},
            {"bucket_name": "u1--host-unreachable", "access_key_id": "k2"},
        ],
    }
    forgotten: list[tuple[str, tuple[str, ...], bool]] = []

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
        is_pruning: bool,
    ) -> None:
        forgotten.append((repository, tuple(snapshot_ids), is_pruning))

    progress_lines: list[str] = []
    is_under, detail = run_backup_trim(
        account_email="a@example.com",
        cli=cli,
        paths=paths,
        report_progress=progress_lines.append,
        list_snapshots_fn=_fake_list_snapshots,
        forget_snapshots_fn=_fake_forget,
    )
    assert is_under is True
    assert cli.cleanup_grant_call_count == 1
    # The oldest snapshot (of three) is forgotten with pruning; the latest survives.
    assert forgotten == [("s3:https://acct.r2.cloudflarestorage.com/u1--host-abc", ("oldest",), True)]
    assert any("removing 1 of 3" in line for line in progress_lines)


def test_run_backup_trim_reports_untrimmable_buckets_when_still_over(tmp_path: Path) -> None:
    """With no local restic env for any bucket, nothing can be forgotten and the run reports why."""
    cli = make_fake_imbue_cloud_cli()
    cli.storage_recheck_results = [{"is_over_quota": True, "usage_bytes": 1000, "limit_bytes": 100}]
    cli.cleanup_grant_result = {
        "status": "granted",
        "keys": [{"bucket_name": "u1--host-elsewhere", "access_key_id": "k1"}],
    }
    is_under, detail = run_backup_trim(
        account_email="a@example.com",
        cli=cli,
        paths=WorkspacePaths(data_dir=tmp_path),
        report_progress=lambda _detail: None,
    )
    assert is_under is False
    assert "u1--host-elsewhere" in detail
    # One grant round is enough to learn nothing is trimmable.
    assert cli.cleanup_grant_call_count == 1
