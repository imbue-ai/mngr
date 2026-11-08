"""Unit tests for the canonical per-workspace restic env store."""

import stat
from datetime import datetime
from datetime import timezone
from pathlib import Path

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import archive_canonical_env
from imbue.minds.desktop_client.backup_env_store import archived_password_for_repository
from imbue.minds.desktop_client.backup_env_store import backup_location_from_env
from imbue.minds.desktop_client.backup_env_store import backup_store_repository_from_env
from imbue.minds.desktop_client.backup_env_store import canonical_env_path
from imbue.minds.desktop_client.backup_env_store import env_content_sha256
from imbue.minds.desktop_client.backup_env_store import full_backup_bucket_name_from_env
from imbue.minds.desktop_client.backup_env_store import has_canonical_env
from imbue.minds.desktop_client.backup_env_store import is_legacy_bucket_repository
from imbue.minds.desktop_client.backup_env_store import list_archived_envs
from imbue.minds.desktop_client.backup_env_store import list_previous_destination_envs
from imbue.minds.desktop_client.backup_env_store import parse_backup_store_repository
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_archived_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import remove_canonical_env_and_archives
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.testing import FAKE_BACKUP_USER_SEGMENT
from imbue.minds.desktop_client.testing import store_repository_url
from imbue.mngr.primitives import AgentId

_WORKSPACE_ID = "agent-" + "a" * 32
_STORE_URL = store_repository_url(_WORKSPACE_ID)


def _paths(tmp_path: Path) -> InstallationPaths:
    return InstallationPaths(data_dir=tmp_path)


def test_parse_backup_store_repository_accepts_only_the_store_shape() -> None:
    parsed = parse_backup_store_repository(_STORE_URL)
    assert parsed is not None
    assert parsed.user_segment == FAKE_BACKUP_USER_SEGMENT
    assert parsed.workspace_id == _WORKSPACE_ID
    assert parsed.path == f"{FAKE_BACKUP_USER_SEGMENT}/{_WORKSPACE_ID}"
    # Without the trailing slash, over http (a dev store), and surrounded by whitespace.
    assert (
        parse_backup_store_repository(f" rest:http://localhost:8787/{FAKE_BACKUP_USER_SEGMENT}/{_WORKSPACE_ID} ")
        is not None
    )
    # A user's own rest-server, a legacy bucket, and a BYO S3 backend are not store repositories.
    assert parse_backup_store_repository("rest:https://example.com/repo") is None
    assert parse_backup_store_repository(f"rest:https://example.com/short/{_WORKSPACE_ID}/") is None
    assert parse_backup_store_repository("s3:https://acct.r2.cloudflarestorage.com/u1--agent-abc") is None


def test_is_legacy_bucket_repository_needs_s3_on_r2() -> None:
    assert is_legacy_bucket_repository("s3:https://acct.r2.cloudflarestorage.com/u1--agent-abc") is True
    assert is_legacy_bucket_repository("s3:https://s3.amazonaws.com/mine") is False
    assert is_legacy_bucket_repository(_STORE_URL) is False


def test_backup_location_from_env_records_store_path_or_legacy_bucket_name() -> None:
    assert (
        backup_location_from_env(f"RESTIC_REPOSITORY={_STORE_URL}\n") == f"{FAKE_BACKUP_USER_SEGMENT}/{_WORKSPACE_ID}"
    )
    assert (
        backup_location_from_env("RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/u1--agent-abc\n")
        == "u1--agent-abc"
    )
    assert backup_location_from_env("RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/mine\n") is None
    assert backup_store_repository_from_env("RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/mine\n") is None


def test_archived_envs_are_listed_newest_first_and_readable_by_archive_id(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=first\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=second\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=current\n")
    # A stray file with a non-timestamp suffix is not an archive.
    (canonical_env_path(paths, agent_id).parent / f"{agent_id}.env.tmp").write_text("junk")

    archives = list_archived_envs(paths, agent_id)
    assert [archive.archive_id for archive in archives] == ["20260201T000000Z", "20260101T000000Z"]
    assert archives[0].archived_at == datetime(2026, 2, 1, tzinfo=timezone.utc)
    assert read_archived_env(paths, agent_id, "20260101T000000Z") == "RESTIC_REPOSITORY=first\n"
    assert read_archived_env(paths, agent_id, "20260301T000000Z") is None
    # An id that is not a bare timestamp never reaches the filesystem.
    assert read_archived_env(paths, agent_id, "../" + str(agent_id) + ".env") is None
    assert list_archived_envs(paths, AgentId()) == []
    assert list_archived_envs(_paths(tmp_path / "missing"), agent_id) == []


def test_remove_canonical_env_and_archives_leaves_other_workspaces_alone(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    other = AgentId()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=first\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=current\n")
    write_canonical_env(paths, other, "RESTIC_REPOSITORY=other\n")

    remove_canonical_env_and_archives(paths, agent_id)
    remove_canonical_env_and_archives(paths, agent_id)

    assert not has_canonical_env(paths, agent_id)
    assert list_archived_envs(paths, agent_id) == []
    assert read_canonical_env(paths, other) == "RESTIC_REPOSITORY=other\n"


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    agent_id = AgentId.generate()
    assert read_canonical_env(_paths(tmp_path), agent_id) is None
    assert has_canonical_env(_paths(tmp_path), agent_id) is False


def test_write_then_read_round_trips(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n")
    assert has_canonical_env(paths, agent_id) is True
    assert read_canonical_env(paths, agent_id) == "RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=p\n"


def test_write_overwrites_existing(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=old\n")
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=new\n")
    assert read_canonical_env(paths, agent_id) == "RESTIC_REPOSITORY=new\n"


def test_written_file_is_owner_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:r\n")
    mode = stat.S_IMODE(canonical_env_path(paths, agent_id).stat().st_mode)
    assert mode == 0o600


def test_distinct_workspaces_get_distinct_files(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = AgentId.generate()
    second = AgentId.generate()
    assert canonical_env_path(paths, first) != canonical_env_path(paths, second)


def test_parse_restic_env_handles_export_quotes_and_comments() -> None:
    parsed = parse_restic_env(
        "# header\nexport RESTIC_REPOSITORY=s3:r\nAWS_ACCESS_KEY_ID=\"a b\"\n\n# c\nRESTIC_PASSWORD='p'\n"
    )
    assert parsed == {
        "RESTIC_REPOSITORY": "s3:r",
        "AWS_ACCESS_KEY_ID": "a b",
        "RESTIC_PASSWORD": "p",
    }


def test_full_backup_bucket_name_from_env_identifies_imbue_cloud_and_byo() -> None:
    r2_env = "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/abc123--agent-9c35\nRESTIC_PASSWORD=pw\n"
    byo_env = "RESTIC_REPOSITORY=s3:https://s3.amazonaws.com/my-own-bucket\nRESTIC_PASSWORD=pw\n"
    separatorless_env = "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/plainbucket\nRESTIC_PASSWORD=pw\n"
    assert full_backup_bucket_name_from_env(r2_env) == "abc123--agent-9c35"
    assert full_backup_bucket_name_from_env(byo_env) is None
    assert full_backup_bucket_name_from_env(separatorless_env) is None


def test_env_content_sha256_is_stable_hex() -> None:
    first = env_content_sha256("RESTIC_REPOSITORY=s3:r\n")
    second = env_content_sha256("RESTIC_REPOSITORY=s3:r\n")
    assert first == second
    assert len(first) == 64
    assert first != env_content_sha256("RESTIC_REPOSITORY=s3:other\n")


def test_archive_canonical_env_moves_the_file_aside(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId.generate()
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:old\nRESTIC_PASSWORD=p\n")
    now = datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc)

    archive_path = archive_canonical_env(paths, agent_id, now=now)

    assert archive_path is not None
    assert archive_path.name == f"{agent_id}.env.20260707T120000Z"
    assert archive_path.read_text() == "RESTIC_REPOSITORY=s3:old\nRESTIC_PASSWORD=p\n"
    assert read_canonical_env(paths, agent_id) is None


def test_previous_destination_envs_leave_out_archives_of_the_current_repository(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    repository = "rest:https://backups.example.com/u/" + _WORKSPACE_ID + "/"
    write_canonical_env(
        paths, agent_id, "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/b\nRESTIC_PASSWORD=legacy\n"
    )
    archive_canonical_env(paths, agent_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    # Backups turned off and on again: the same repository, archived without its trailing slash.
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository.rstrip('/')}\nRESTIC_PASSWORD=old\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD=old\n")

    assert [archive.archive_id for archive in list_previous_destination_envs(paths, agent_id)] == ["20260101T000000Z"]
    assert list_previous_destination_envs(paths, AgentId.generate()) == []


def test_archived_password_for_repository_reads_the_newest_matching_archive(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    repository = "rest:https://backups.example.com/u/" + _WORKSPACE_ID + "/"
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository}\nRESTIC_PASSWORD=first\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    write_canonical_env(
        paths, agent_id, "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/b\nRESTIC_PASSWORD=legacy\n"
    )
    archive_canonical_env(paths, agent_id, now=datetime(2026, 2, 1, tzinfo=timezone.utc))
    # The same repository without its trailing slash, keyed later.
    write_canonical_env(paths, agent_id, f"RESTIC_REPOSITORY={repository.rstrip('/')}\nRESTIC_PASSWORD=second\n")
    archive_canonical_env(paths, agent_id, now=datetime(2026, 3, 1, tzinfo=timezone.utc))

    assert archived_password_for_repository(paths, agent_id, repository) == "second"
    assert archived_password_for_repository(paths, agent_id, "rest:https://backups.example.com/u/agent-other/") is None
    assert archived_password_for_repository(paths, AgentId.generate(), repository) is None


def test_archive_canonical_env_returns_none_when_absent(tmp_path: Path) -> None:
    assert archive_canonical_env(_paths(tmp_path), AgentId.generate(), now=datetime.now(timezone.utc)) is None
