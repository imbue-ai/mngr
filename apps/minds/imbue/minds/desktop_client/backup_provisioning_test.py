"""Unit tests for the pure / cli-driven logic in backup_provisioning.

The remote-injection (``mngr exec``) and restic-init paths are exercised by
the local-restic integration test (``restic_cli_test.py``) and release tests
against a real agent; here we cover the pure helpers, repo/creds resolution,
and the bucket idempotency branch with a canned cli.
"""

import pytest
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backup_env_store import parse_restic_env
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import _create_or_reuse_bucket
from imbue.minds.desktop_client.backup_provisioning import _is_bucket_already_exists_error
from imbue.minds.desktop_client.backup_provisioning import _repository_url_for_bucket
from imbue.minds.desktop_client.backup_provisioning import _resolve_repository_and_backend_env
from imbue.minds.desktop_client.backup_provisioning import build_canonical_env_content
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.backup_provisioning import generate_workspace_password
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCli
from imbue.minds.desktop_client.imbue_cloud_cli import ImbueCloudCliError
from imbue.minds.desktop_client.imbue_cloud_cli import R2BucketCreateResult
from imbue.minds.desktop_client.imbue_cloud_cli import R2BucketInfo
from imbue.minds.desktop_client.imbue_cloud_cli import R2BucketKeyMaterial
from imbue.minds.errors import BackupProvisioningError
from imbue.minds.primitives import BackupProvider

_ENDPOINT = "https://acct.r2.cloudflarestorage.com"


def _fake_key(bucket_name: str) -> R2BucketKeyMaterial:
    return R2BucketKeyMaterial(
        access_key_id="AKID",
        secret_access_key=SecretStr("SECRET"),
        s3_endpoint=AnyUrl(_ENDPOINT),
        bucket_name=bucket_name,
        access="readwrite",
    )


class _FakeImbueCloudCli(ImbueCloudCli):
    """Canned cli that returns bucket data without spawning subprocesses."""

    create_error_stderr: str | None = Field(default=None)
    created_names: list[str] = Field(default_factory=list)
    minted_key_names: list[str] = Field(default_factory=list)

    def create_bucket(self, *, account: str, name: str, access: str = "readwrite") -> R2BucketCreateResult:
        self.created_names.append(name)
        if self.create_error_stderr is not None:
            error = ImbueCloudCliError("bucket create failed")
            error.stderr = self.create_error_stderr
            raise error
        full = f"u--{name}"
        return R2BucketCreateResult(
            bucket=R2BucketInfo(bucket_name=full, s3_endpoint=AnyUrl(_ENDPOINT)),
            key=_fake_key(full),
        )

    def get_bucket_info(self, account: str, name: str) -> R2BucketInfo:
        return R2BucketInfo(bucket_name=f"u--{name}", s3_endpoint=AnyUrl(_ENDPOINT))

    def create_bucket_key(
        self, *, account: str, name: str, access: str = "readwrite", alias: str | None = None
    ) -> R2BucketKeyMaterial:
        self.minted_key_names.append(name)
        return _fake_key(f"u--{name}")


def _make_cli(*, create_error_stderr: str | None = None) -> _FakeImbueCloudCli:
    return _FakeImbueCloudCli(
        parent_concurrency_group=ConcurrencyGroup(name="test-backup-cli"),
        connector_url=AnyUrl("http://connector.example"),
        create_error_stderr=create_error_stderr,
    )


# --- env_text_defines_restic_password ---


def test_env_text_defines_restic_password_detects_plain_and_export() -> None:
    assert env_text_defines_restic_password("RESTIC_PASSWORD=x\n") is True
    assert env_text_defines_restic_password("export RESTIC_PASSWORD=x\n") is True


def test_env_text_defines_restic_password_ignores_comment_and_absence() -> None:
    assert env_text_defines_restic_password("# RESTIC_PASSWORD=x\n") is False
    assert env_text_defines_restic_password("AWS_ACCESS_KEY_ID=k\n") is False


# --- generate_workspace_password ---


def test_generate_workspace_password_is_long_and_unique() -> None:
    first = generate_workspace_password()
    second = generate_workspace_password()
    assert len(first) >= 32
    assert first != second


# --- build_canonical_env_content ---


def test_build_canonical_env_content_round_trips() -> None:
    content = build_canonical_env_content(
        repository="s3:https://acct/u--host-1",
        backend_env={"AWS_ACCESS_KEY_ID": "AK", "AWS_SECRET_ACCESS_KEY": "SK"},
        workspace_password="rndpw",
    )
    parsed = parse_restic_env(content)
    assert parsed["RESTIC_REPOSITORY"] == "s3:https://acct/u--host-1"
    assert parsed["AWS_ACCESS_KEY_ID"] == "AK"
    assert parsed["AWS_SECRET_ACCESS_KEY"] == "SK"
    assert parsed["RESTIC_PASSWORD"] == "rndpw"


# --- _repository_url_for_bucket ---


def test_repository_url_strips_trailing_slash_and_points_at_bucket_root() -> None:
    assert _repository_url_for_bucket(_ENDPOINT + "/", "u--host-1") == f"s3:{_ENDPOINT}/u--host-1"


# --- _is_bucket_already_exists_error ---


def test_is_bucket_already_exists_error_matches_structured_and_prose() -> None:
    structured = ImbueCloudCliError("x")
    structured.stderr = '{"error_class": "ImbueCloudBucketExistsError"}'
    assert _is_bucket_already_exists_error(structured) is True
    prose = ImbueCloudCliError("bucket already exists")
    assert _is_bucket_already_exists_error(prose) is True
    other = ImbueCloudCliError("internal error")
    other.stderr = '{"error": "boom"}'
    assert _is_bucket_already_exists_error(other) is False


# --- _create_or_reuse_bucket ---


def test_create_or_reuse_creates_a_fresh_bucket() -> None:
    cli = _make_cli()
    bucket_name, endpoint, key = _create_or_reuse_bucket(cli, "a@b.com", "host-abc")
    assert bucket_name == "u--host-abc"
    assert endpoint == _ENDPOINT + "/"
    assert key.access_key_id == "AKID"
    assert cli.created_names == ["host-abc"]
    assert cli.minted_key_names == []


def test_create_or_reuse_reuses_existing_bucket_with_fresh_key() -> None:
    cli = _make_cli(create_error_stderr='{"error": "bucket already exists"}')
    bucket_name, _endpoint, key = _create_or_reuse_bucket(cli, "a@b.com", "host-abc")
    assert bucket_name == "u--host-abc"
    assert cli.minted_key_names == ["host-abc"]
    assert key.secret_access_key.get_secret_value() == "SECRET"


def test_create_or_reuse_propagates_non_exists_errors() -> None:
    cli = _make_cli(create_error_stderr='{"error": "internal error"}')
    with pytest.raises(ImbueCloudCliError):
        _create_or_reuse_bucket(cli, "a@b.com", "host-abc")


# --- _resolve_repository_and_backend_env ---


def test_resolve_imbue_cloud_builds_repo_and_creds() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    repository, backend_env = _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=_make_cli())
    assert repository == f"s3:{_ENDPOINT}/u--host-abc"
    assert backend_env == {"AWS_ACCESS_KEY_ID": "AKID", "AWS_SECRET_ACCESS_KEY": "SECRET"}


def test_resolve_imbue_cloud_requires_cli() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=None)


def test_resolve_imbue_cloud_requires_account() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=_make_cli())


def test_resolve_api_key_extracts_repo_and_backend_env() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.API_KEY,
        api_key_env_text="RESTIC_REPOSITORY=s3:r\nAWS_ACCESS_KEY_ID=k\nAWS_SECRET_ACCESS_KEY=s\n",
    )
    repository, backend_env = _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=None)
    assert repository == "s3:r"
    assert backend_env == {"AWS_ACCESS_KEY_ID": "k", "AWS_SECRET_ACCESS_KEY": "s"}


def test_resolve_api_key_rejects_restic_password() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.API_KEY,
        api_key_env_text="RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=nope\n",
    )
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=None)


def test_resolve_api_key_requires_repository() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.API_KEY, api_key_env_text="AWS_ACCESS_KEY_ID=k\n")
    with pytest.raises(BackupProvisioningError):
        _resolve_repository_and_backend_env(request, "host-abc", imbue_cloud_cli=None)
