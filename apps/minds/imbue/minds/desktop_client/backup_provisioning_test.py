"""Unit tests for the pure / cli-driven logic in backup_provisioning.

The remote-injection (``mngr exec``) and restic-init paths are exercised by
the local-restic integration test (``restic_cli_test.py``) and release tests
against a real agent; here we cover the pure helpers, repo/creds resolution,
and the quota-eviction retry with a canned cli.
"""

import os
import subprocess
from pathlib import Path

import pytest
from pydantic import AnyUrl
from pydantic import Field

from imbue.minds.config.data_types import InstallationPaths
from imbue.minds.desktop_client.backup_env_store import archived_password_for_repository
from imbue.minds.desktop_client.backup_env_store import backup_env_dir
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_env_store import read_canonical_env
from imbue.minds.desktop_client.backup_env_store import write_canonical_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import _create_repository_evicting_on_quota
from imbue.minds.desktop_client.backup_provisioning import _resolve_repository_and_backend_env
from imbue.minds.desktop_client.backup_provisioning import build_backup_exec_argv
from imbue.minds.desktop_client.backup_provisioning import build_canonical_env_content
from imbue.minds.desktop_client.backup_provisioning import build_write_file_command
from imbue.minds.desktop_client.backup_provisioning import change_backup_destination_for_host
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.backup_provisioning import generate_workspace_password
from imbue.minds.desktop_client.conftest import FakeImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import BackupRepositoryCredentials
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudQuotaExceededCliError
from imbue.minds.desktop_client.testing import store_repository_url
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider
from imbue.mngr.primitives import AgentId

_WORKSPACE_ID = "agent-" + "a" * 32


def _make_cli() -> FakeImbueCloudCli:
    return FakeImbueCloudCli(connector_url=AnyUrl("http://connector.example"))


def test_env_text_defines_restic_password_detects_plain_and_export() -> None:
    assert env_text_defines_restic_password("RESTIC_PASSWORD=x\n") is True
    assert env_text_defines_restic_password("export RESTIC_PASSWORD=x\n") is True


def test_env_text_defines_restic_password_ignores_comment_and_absence() -> None:
    assert env_text_defines_restic_password("# RESTIC_PASSWORD=x\n") is False
    assert env_text_defines_restic_password("AWS_ACCESS_KEY_ID=k\n") is False


def test_generate_workspace_password_is_long_and_unique() -> None:
    first = generate_workspace_password()
    second = generate_workspace_password()
    assert len(first) >= 32
    assert first != second


def test_build_canonical_env_content_round_trips() -> None:
    content = build_canonical_env_content(
        repository=store_repository_url(_WORKSPACE_ID),
        backend_env={"RESTIC_REST_USERNAME": _WORKSPACE_ID, "RESTIC_REST_PASSWORD": "tok"},
        workspace_password="rndpw",
    )
    parsed = parse_restic_env(content)
    assert parsed["RESTIC_REPOSITORY"] == store_repository_url(_WORKSPACE_ID)
    assert parsed["RESTIC_REST_USERNAME"] == _WORKSPACE_ID
    assert parsed["RESTIC_REST_PASSWORD"] == "tok"
    assert parsed["RESTIC_PASSWORD"] == "rndpw"


def _run_write_file_command(cwd: Path, content: str, rotate_timestamp: str | None) -> None:
    command = build_write_file_command(
        "data/.secrets/restic.env", content, mode="600", rotate_timestamp=rotate_timestamp
    )
    subprocess.run(["sh", "-c", command], cwd=cwd, check=True, timeout=30)


def test_write_file_command_leaves_an_identical_file_untouched_and_rotates_a_different_one(tmp_path: Path) -> None:
    env_path = tmp_path / "data" / ".secrets" / "restic.env"
    _run_write_file_command(tmp_path, "RESTIC_PASSWORD=one\n", "20260925T000000Z")
    assert env_path.read_text() == "RESTIC_PASSWORD=one\n"
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"
    os.utime(env_path, (1_000_000_000, 1_000_000_000))
    env_path.chmod(0o644)

    _run_write_file_command(tmp_path, "RESTIC_PASSWORD=one\n", "20260925T000001Z")
    assert env_path.stat().st_mtime == 1_000_000_000
    assert oct(env_path.stat().st_mode & 0o777) == "0o600"
    assert sorted(path.name for path in env_path.parent.iterdir()) == ["restic.env"]

    _run_write_file_command(tmp_path, "RESTIC_PASSWORD=two\n", "20260925T000002Z")
    assert env_path.read_text() == "RESTIC_PASSWORD=two\n"
    assert (env_path.parent / "restic.env.20260925T000002Z").read_text() == "RESTIC_PASSWORD=one\n"

    # Without a rotation timestamp a differing file is overwritten in place.
    _run_write_file_command(tmp_path, "RESTIC_PASSWORD=three\n", None)
    assert env_path.read_text() == "RESTIC_PASSWORD=three\n"
    assert sorted(path.name for path in env_path.parent.iterdir()) == ["restic.env", "restic.env.20260925T000002Z"]


def test_create_repository_mints_credentials_for_the_workspace() -> None:
    cli = _make_cli()
    credentials = _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, None)
    assert credentials.workspace_id == _WORKSPACE_ID
    assert credentials.rest_username == _WORKSPACE_ID
    assert credentials.repository_url == store_repository_url(_WORKSPACE_ID)
    assert cli.created_backup_repositories == [_WORKSPACE_ID]


def test_create_repository_is_idempotent_and_mints_a_fresh_token_each_time() -> None:
    cli = _make_cli()
    first = _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, None)
    second = _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, None)
    assert first.rest_password.get_secret_value() != second.rest_password.get_secret_value()
    assert len(cli.backup_repositories) == 1


def test_resolve_imbue_cloud_builds_rest_repo_and_creds() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    repository, backend_env = _resolve_repository_and_backend_env(
        request, _WORKSPACE_ID, imbue_cloud_cli=_make_cli(), quota_evictor=None
    )
    assert repository == store_repository_url(_WORKSPACE_ID)
    assert backend_env == {"RESTIC_REST_USERNAME": _WORKSPACE_ID, "RESTIC_REST_PASSWORD": "fake-token-1"}


def test_resolve_imbue_cloud_requires_cli() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, _WORKSPACE_ID, imbue_cloud_cli=None, quota_evictor=None)


def test_resolve_imbue_cloud_requires_account() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, _WORKSPACE_ID, imbue_cloud_cli=_make_cli(), quota_evictor=None)


def test_resolve_imbue_cloud_requires_workspace_id() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, "", imbue_cloud_cli=_make_cli(), quota_evictor=None)


def test_change_backup_destination_restores_the_previous_env_when_provisioning_fails(tmp_path: Path) -> None:
    # The old env is archived before provisioning; a failed change (here:
    # imbue_cloud without a CLI) must put it back rather than leave the
    # workspace with no canonical env, which the desktop reads as "backups off"
    # and the legacy-to-store migration never retries.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    legacy_env = (
        "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/prefix--"
        + _WORKSPACE_ID
        + "\nRESTIC_PASSWORD=pw\n"
    )
    write_canonical_env(paths, agent_id, legacy_env)
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")

    with pytest.raises(BackupProvisioningError):
        change_backup_destination_for_host(
            agent_id=agent_id, agent_address=f"{agent_id}@host", request=request, imbue_cloud_cli=None, paths=paths
        )

    assert read_canonical_env(paths, agent_id) == legacy_env
    assert sorted(path.name for path in backup_env_dir(paths).iterdir()) == [f"{agent_id}.env"]


def test_change_backup_destination_asks_for_fresh_provisioning(tmp_path: Path) -> None:
    # The record sync can put a missing canonical env back between the archive
    # and the provisioning; the change must provision fresh regardless, or it
    # would re-inject the old env and report the destination as changed.
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    write_canonical_env(paths, agent_id, "RESTIC_REPOSITORY=s3:https://old/bucket\nRESTIC_PASSWORD=pw\n")
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    configure_calls: list[dict[str, object]] = []

    def recording_configure(**kwargs: object) -> None:
        configure_calls.append(kwargs)

    change_backup_destination_for_host(
        agent_id=agent_id,
        agent_address=f"{agent_id}@host",
        request=request,
        imbue_cloud_cli=None,
        paths=paths,
        configure_fn=recording_configure,
    )

    (call,) = configure_calls
    assert call["is_existing_env_reused"] is False
    assert call["request"] is request


@pytest.mark.parametrize("is_rolled_back_on_failure", [True, False])
def test_change_backup_destination_rolls_back_a_half_applied_change_only_when_asked(
    tmp_path: Path, is_rolled_back_on_failure: bool
) -> None:
    # Provisioning writes the new canonical env before it injects it; a change
    # that fails at injection keeps the new env by default (Imbue Studio can
    # still reach the repository) and puts the old one back for a caller that
    # retries from scratch (the legacy-to-store migration).
    paths = InstallationPaths(data_dir=tmp_path)
    agent_id = AgentId(_WORKSPACE_ID)
    legacy_env = (
        "RESTIC_REPOSITORY=s3:https://acct.r2.cloudflarestorage.com/prefix--"
        + _WORKSPACE_ID
        + "\nRESTIC_PASSWORD=pw\n"
    )
    store_env = "RESTIC_REPOSITORY=rest:https://backups.example.com/u/" + _WORKSPACE_ID + "/\nRESTIC_PASSWORD=pw2\n"
    write_canonical_env(paths, agent_id, legacy_env)
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")

    def half_applied_configure(*, agent_id: AgentId, paths: InstallationPaths, **_kwargs: object) -> None:
        write_canonical_env(paths, agent_id, store_env)
        raise BackupProvisioningError("injection failed")

    with pytest.raises(BackupProvisioningError, match="injection failed"):
        change_backup_destination_for_host(
            agent_id=agent_id,
            agent_address=f"{agent_id}@host",
            request=request,
            imbue_cloud_cli=None,
            paths=paths,
            is_rolled_back_on_failure=is_rolled_back_on_failure,
            configure_fn=half_applied_configure,
        )

    env_names = sorted(path.name for path in backup_env_dir(paths).iterdir())
    assert len(env_names) == 2
    if is_rolled_back_on_failure:
        assert read_canonical_env(paths, agent_id) == legacy_env
        # The half-applied store env is kept as an archive: it holds the
        # password the store repository was keyed with, which the retry reuses.
        archived_store_env = (backup_env_dir(paths) / env_names[1]).read_text()
        assert archived_store_env == store_env
        assert (
            archived_password_for_repository(
                paths, agent_id, "rest:https://backups.example.com/u/" + _WORKSPACE_ID + "/"
            )
            == "pw2"
        )
    else:
        assert read_canonical_env(paths, agent_id) == store_env


def test_resolve_api_key_extracts_repo_and_backend_env() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.API_KEY,
        api_key_env_text="RESTIC_REPOSITORY=s3:r\nAWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n",
    )
    repository, backend_env = _resolve_repository_and_backend_env(
        request, _WORKSPACE_ID, imbue_cloud_cli=None, quota_evictor=None
    )
    assert repository == "s3:r"
    assert backend_env == {"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"}


def test_resolve_api_key_rejects_restic_password() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.API_KEY,
        api_key_env_text="RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=nope\n",
    )
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, _WORKSPACE_ID, imbue_cloud_cli=None, quota_evictor=None)


def test_resolve_api_key_requires_repository() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.API_KEY, api_key_env_text="AWS_ACCESS_KEY_ID=k\n")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, _WORKSPACE_ID, imbue_cloud_cli=None, quota_evictor=None)


def test_backup_exec_argv_never_starts_a_stopped_host() -> None:
    """Every backup-stack exec -- verification check, env injection, update
    scripts -- must pass --no-start: ``mngr exec`` auto-starts the host by
    default, and the periodic backup checker was observed cold-booting a
    stopped container it believed online off a stale RUNNING snapshot.
    """
    argv = build_backup_exec_argv(AgentId.generate(), "echo hi")
    assert argv[1] == "exec"
    assert "--no-start" in argv


class _QuotaThenSuccessCli(FakeImbueCloudCli):
    """Raises the typed quota error until ``quota_failures`` creates have been attempted."""

    quota_failures: int = Field(default=1)
    attempt_count: int = Field(default=0)

    def create_backup_repository(self, *, account: str, workspace_id: str) -> BackupRepositoryCredentials:
        self.attempt_count += 1
        if self.attempt_count <= self.quota_failures:
            raise ImbueCloudQuotaExceededCliError("backup-repo create: quota exceeded")
        return super().create_backup_repository(account=account, workspace_id=workspace_id)


def test_create_repository_evicts_and_retries_on_quota_pressure() -> None:
    cli = _QuotaThenSuccessCli(connector_url=AnyUrl("http://connector.example"), quota_failures=2)
    evictions: list[int] = []

    def evictor() -> bool:
        evictions.append(1)
        return True

    credentials = _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, evictor)

    assert credentials.workspace_id == _WORKSPACE_ID
    assert len(evictions) == 2
    assert cli.attempt_count == 3


def test_create_repository_raises_quota_error_when_nothing_to_evict() -> None:
    cli = _QuotaThenSuccessCli(connector_url=AnyUrl("http://connector.example"), quota_failures=99)

    with pytest.raises(ImbueCloudQuotaExceededCliError):
        _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, lambda: False)

    # The failed eviction aborts immediately: exactly one create was attempted.
    assert cli.attempt_count == 1


def test_create_repository_raises_quota_error_without_an_evictor() -> None:
    cli = _QuotaThenSuccessCli(connector_url=AnyUrl("http://connector.example"), quota_failures=1)
    with pytest.raises(ImbueCloudQuotaExceededCliError):
        _create_repository_evicting_on_quota(cli, "a@b.com", _WORKSPACE_ID, None)
