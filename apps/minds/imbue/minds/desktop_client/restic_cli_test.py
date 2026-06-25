"""Unit + local-restic integration tests for the minds restic wrapper.

restic is a required dependency of the minds app (and is installed in the
test images), so the integration tests run unconditionally and FAIL -- not
skip -- if the ``restic`` binary is missing.
"""

from datetime import datetime
from datetime import timezone
from pathlib import Path

import pytest

from imbue.minds.desktop_client import restic_cli
from imbue.minds.desktop_client.restic_cli import _env_and_flags
from imbue.minds.desktop_client.restic_cli import _looks_already_initialized
from imbue.minds.desktop_client.restic_cli import parse_restic_snapshots
from imbue.minds.desktop_client.restic_cli import parse_restic_timestamp
from imbue.minds.desktop_client.testing import restic_backup_a_file
from imbue.minds.errors import BackupProvisioningError

# --- parse_restic_timestamp ---


def test_parse_restic_timestamp_handles_z_and_nanoseconds() -> None:
    parsed = parse_restic_timestamp("2026-05-29T05:33:16.123456789Z")
    assert parsed is not None
    assert parsed.tzinfo is not None
    assert parsed.year == 2026 and parsed.minute == 33


def test_parse_restic_timestamp_handles_offset() -> None:
    parsed = parse_restic_timestamp("2026-05-29T05:33:16+02:00")
    assert parsed is not None
    # Normalized to UTC.
    offset = parsed.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 0


def test_parse_restic_timestamp_assumes_utc_when_naive() -> None:
    parsed = parse_restic_timestamp("2026-05-29T05:33:16")
    assert parsed is not None
    assert parsed.tzinfo is not None


def test_parse_restic_timestamp_returns_none_on_garbage() -> None:
    assert parse_restic_timestamp("") is None
    assert parse_restic_timestamp("not-a-time") is None


# --- _env_and_flags ---


def test_env_and_flags_with_password_sets_env_no_flag() -> None:
    env, flags = _env_and_flags("s3:r", {"AWS_ACCESS_KEY_ID": "k"}, "secret")
    assert env["RESTIC_REPOSITORY"] == "s3:r"
    assert env["AWS_ACCESS_KEY_ID"] == "k"
    assert env["RESTIC_PASSWORD"] == "secret"
    assert flags == []


def test_env_and_flags_with_empty_password_uses_insecure_flag() -> None:
    env, flags = _env_and_flags("s3:r", {}, "")
    assert "RESTIC_PASSWORD" not in env
    assert flags == ["--insecure-no-password"]
    env_none, flags_none = _env_and_flags("s3:r", {}, None)
    assert "RESTIC_PASSWORD" not in env_none
    assert flags_none == ["--insecure-no-password"]


# --- _looks_already_initialized ---


def test_looks_already_initialized_matches_common_phrases() -> None:
    assert _looks_already_initialized("Fatal: repository master key already initialized") is True
    assert _looks_already_initialized("config file already exists") is True
    assert _looks_already_initialized("network timeout") is False


# --- transient-auth detection + bounded retry ---


def test_looks_like_transient_auth_failure_matches_known_signals() -> None:
    assert restic_cli._looks_like_transient_auth_failure("Fatal: open repository failed: Unauthorized") is True
    assert restic_cli._looks_like_transient_auth_failure("InvalidAccessKeyId: key is not valid") is True
    assert restic_cli._looks_like_transient_auth_failure("SignatureDoesNotMatch") is True
    assert restic_cli._looks_like_transient_auth_failure("Fatal: network unreachable") is False
    assert restic_cli._looks_like_transient_auth_failure("repository master key already initialized") is False


def test_raise_restic_failure_raises_transient_for_auth_errors() -> None:
    with pytest.raises(restic_cli.ResticTransientAuthError):
        restic_cli._raise_restic_failure("restic init", 1, "Fatal: create repository failed: Unauthorized")


def test_raise_restic_failure_raises_fatal_for_other_errors() -> None:
    with pytest.raises(BackupProvisioningError) as exc_info:
        restic_cli._raise_restic_failure("restic init", 1, "Fatal: host unreachable")
    # A non-auth failure must be the plain (non-retryable) error, not the transient subclass.
    assert not isinstance(exc_info.value, restic_cli.ResticTransientAuthError)


def test_retry_on_transient_auth_retries_until_success() -> None:
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        if len(attempts) < 3:
            raise restic_cli.ResticTransientAuthError("Unauthorized")
        return "ok"

    result = restic_cli._retry_on_transient_auth(operation, timeout_seconds=5.0, wait_seconds=0.01)
    assert result == "ok"
    assert len(attempts) == 3


def test_retry_on_transient_auth_reraises_after_timeout() -> None:
    def operation() -> str:
        raise restic_cli.ResticTransientAuthError("Unauthorized")

    with pytest.raises(restic_cli.ResticTransientAuthError):
        restic_cli._retry_on_transient_auth(operation, timeout_seconds=0.05, wait_seconds=0.01)


def test_retry_on_transient_auth_does_not_retry_fatal_errors() -> None:
    attempts: list[int] = []

    def operation() -> str:
        attempts.append(1)
        raise BackupProvisioningError("fatal")

    with pytest.raises(BackupProvisioningError):
        restic_cli._retry_on_transient_auth(operation, timeout_seconds=5.0, wait_seconds=0.01)
    # A fatal (non-transient) error must not be retried.
    assert len(attempts) == 1


# --- ensure_restic_available ---


def test_ensure_restic_available_does_not_raise() -> None:
    # restic is a required dependency: this must pass in every test env.
    restic_cli.ensure_restic_available()


# --- local restic integration ---


@pytest.mark.timeout(60)
def test_init_add_key_and_status_against_local_repo(tmp_path: Path) -> None:
    repo = str(tmp_path / "repo")
    master = "master-passphrase"
    workspace_password = "workspace-random-key"

    # Init with the master password, then add the random workspace key.
    restic_cli.init_repo(repository=repo, backend_env={}, password=master)
    restic_cli.add_password_key(
        repository=repo, backend_env={}, existing_password=master, new_password=workspace_password
    )

    now = datetime.now(timezone.utc)
    # Fresh repo: no snapshots, no in-progress lock -- queried with the
    # workspace key (proving the added key opens the repo).
    assert restic_cli.get_latest_snapshot_time(repository=repo, backend_env={}, password=workspace_password) is None
    assert (
        restic_cli.is_backup_in_progress(repository=repo, backend_env={}, password=workspace_password, now=now)
        is False
    )

    # After a backup, the latest-snapshot time is populated.
    source = tmp_path / "data.txt"
    source.write_text("hello backup")
    restic_backup_a_file(repo, workspace_password, source)
    latest = restic_cli.get_latest_snapshot_time(repository=repo, backend_env={}, password=workspace_password)
    assert latest is not None
    assert latest.tzinfo is not None


# --- parse_restic_snapshots ---


def test_parse_restic_snapshots_parses_fields_summary_and_tags() -> None:
    stdout = """[
      {
        "time": "2026-05-29T05:33:16.123456789Z",
        "paths": ["/data", "/runtime"],
        "hostname": "workspace-host",
        "tags": ["hourly"],
        "id": "aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888",
        "short_id": "aaaa1111",
        "summary": {"total_size": 4096}
      }
    ]"""

    snapshots = parse_restic_snapshots(stdout)

    assert len(snapshots) == 1
    snapshot = snapshots[0]
    assert snapshot.snapshot_id == "aaaa1111bbbb2222cccc3333dddd4444eeee5555ffff6666aaaa7777bbbb8888"
    assert snapshot.short_id == "aaaa1111"
    assert snapshot.paths == ("/data", "/runtime")
    assert snapshot.hostname == "workspace-host"
    assert snapshot.tags == ("hourly",)
    assert snapshot.total_size_bytes == 4096
    assert snapshot.time.tzinfo is not None


def test_parse_restic_snapshots_defaults_short_id_and_size_when_absent() -> None:
    stdout = """[
      {"time": "2026-05-29T05:33:16Z", "id": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"}
    ]"""

    snapshots = parse_restic_snapshots(stdout)

    assert len(snapshots) == 1
    assert snapshots[0].short_id == "01234567"
    assert snapshots[0].paths == ()
    assert snapshots[0].tags == ()
    assert snapshots[0].total_size_bytes is None


def test_parse_restic_snapshots_skips_entries_missing_id_or_time() -> None:
    stdout = """[
      {"time": "2026-05-29T05:33:16Z"},
      {"id": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef", "time": "not-a-time"},
      {"id": "feed0000feed0000feed0000feed0000feed0000feed0000feed0000feed0000", "time": "2026-05-29T06:00:00Z"}
    ]"""

    snapshots = parse_restic_snapshots(stdout)

    assert len(snapshots) == 1
    assert snapshots[0].snapshot_id == "feed0000feed0000feed0000feed0000feed0000feed0000feed0000feed0000"


def test_parse_restic_snapshots_handles_empty_output() -> None:
    assert parse_restic_snapshots("") == ()
    assert parse_restic_snapshots("[]") == ()


def test_parse_restic_snapshots_raises_on_non_list_payload() -> None:
    with pytest.raises(BackupProvisioningError):
        parse_restic_snapshots('{"not": "a list"}')


def test_parse_restic_snapshots_raises_on_non_json() -> None:
    with pytest.raises(BackupProvisioningError):
        parse_restic_snapshots("this is not json")


@pytest.mark.timeout(60)
def test_list_snapshots_against_local_repo(tmp_path: Path) -> None:
    repo = str(tmp_path / "repo")
    password = "list-test-pw"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)

    # No snapshots yet.
    assert restic_cli.list_snapshots(repository=repo, backend_env={}, password=password) == ()

    source = tmp_path / "data.txt"
    source.write_text("hello list")
    restic_backup_a_file(repo, password, source)

    snapshots = restic_cli.list_snapshots(repository=repo, backend_env={}, password=password)
    assert len(snapshots) == 1
    assert len(snapshots[0].snapshot_id) == 64
    assert snapshots[0].time.tzinfo is not None


@pytest.mark.timeout(60)
def test_init_repo_is_idempotent_on_existing_repo(tmp_path: Path) -> None:
    repo = str(tmp_path / "repo")
    restic_cli.init_repo(repository=repo, backend_env={}, password="pw")
    # Initializing again must not raise (already-initialized is treated as success).
    restic_cli.init_repo(repository=repo, backend_env={}, password="pw")


@pytest.mark.timeout(60)
def test_restore_snapshot_restores_files(tmp_path: Path) -> None:
    repo = str(tmp_path / "repo")
    password = "restore-test-pw"
    restic_cli.init_repo(repository=repo, backend_env={}, password=password)
    source = tmp_path / "data.txt"
    source.write_text("hello export")
    restic_backup_a_file(repo, password, source)

    restore_dir = tmp_path / "restore"
    restic_cli.restore_snapshot(repository=repo, backend_env={}, password=password, target_dir=restore_dir)

    restored = list(restore_dir.rglob("data.txt"))
    assert restored, list(restore_dir.rglob("*"))
    assert restored[0].read_text() == "hello export"
