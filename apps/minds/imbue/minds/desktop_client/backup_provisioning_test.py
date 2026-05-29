"""Unit tests for the pure / cli-driven logic in backup_provisioning.

These cover the pure plan computation, the bucket idempotency branch, and
the request->plan dispatch with a canned cli. The remote-injection
(``mngr exec``) path is not unit-tested here because it requires a live
agent host.
"""

import pytest
from pydantic import AnyUrl
from pydantic import Field
from pydantic import SecretStr

from imbue.concurrency_group.concurrency_group import ConcurrencyGroup
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.backup_provisioning import _create_or_reuse_bucket
from imbue.minds.desktop_client.backup_provisioning import _plan_for_request
from imbue.minds.desktop_client.backup_provisioning import _repository_url_for_bucket
from imbue.minds.desktop_client.backup_provisioning import build_api_key_restic_env
from imbue.minds.desktop_client.backup_provisioning import build_imbue_cloud_restic_env
from imbue.minds.desktop_client.backup_provisioning import env_text_defines_restic_password
from imbue.minds.desktop_client.backup_provisioning import merge_allow_empty_password_into_backup_toml
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
    """Canned cli that returns bucket data without spawning subprocesses.

    ``create_error_stderr`` makes ``create_bucket`` raise an
    ``ImbueCloudCliError`` carrying that stderr (used to exercise both the
    "already exists" reuse branch and the propagate-other-errors branch).
    """

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


# --- build_imbue_cloud_restic_env ---


def test_build_imbue_cloud_with_master_password_sets_password_and_no_empty_flag() -> None:
    plan = build_imbue_cloud_restic_env(
        repository="s3:https://acct/u--host-1",
        access_key_id="AKID",
        secret_access_key="SECRET",
        master_password="pw",
    )
    assert plan.allow_empty_password is False
    assert "RESTIC_REPOSITORY=s3:https://acct/u--host-1\n" in plan.restic_env_content
    assert "AWS_ACCESS_KEY_ID=AKID\n" in plan.restic_env_content
    assert "AWS_SECRET_ACCESS_KEY=SECRET\n" in plan.restic_env_content
    assert "RESTIC_PASSWORD=pw\n" in plan.restic_env_content


def test_build_imbue_cloud_without_master_password_requests_empty_password() -> None:
    plan = build_imbue_cloud_restic_env(
        repository="s3:r",
        access_key_id="AKID",
        secret_access_key="SECRET",
        master_password=None,
    )
    assert plan.allow_empty_password is True
    assert "RESTIC_PASSWORD" not in plan.restic_env_content


# --- build_api_key_restic_env (password precedence) ---


def test_build_api_key_textarea_password_wins_over_master() -> None:
    plan = build_api_key_restic_env(
        env_text="RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=fromtextarea\n",
        master_password="ignored-master",
    )
    assert plan.allow_empty_password is False
    assert "RESTIC_PASSWORD=fromtextarea" in plan.restic_env_content
    assert "ignored-master" not in plan.restic_env_content


def test_build_api_key_appends_master_password_when_textarea_omits_it() -> None:
    plan = build_api_key_restic_env(env_text="RESTIC_REPOSITORY=s3:r\n", master_password="frommaster")
    assert plan.allow_empty_password is False
    assert plan.restic_env_content.endswith("RESTIC_PASSWORD=frommaster\n")


def test_build_api_key_empty_password_when_no_password_anywhere() -> None:
    plan = build_api_key_restic_env(env_text="RESTIC_REPOSITORY=s3:r\n", master_password=None)
    assert plan.allow_empty_password is True
    assert "RESTIC_PASSWORD" not in plan.restic_env_content


# --- merge_allow_empty_password_into_backup_toml ---


def test_merge_preserves_other_sections() -> None:
    existing = '[snapshot]\nmethod = "DIRECT"\n\n[retention]\nkeep_hourly = 99\n'
    merged = merge_allow_empty_password_into_backup_toml(existing, True)
    assert "allow_empty_password = true" in merged
    assert '[snapshot]' in merged
    assert "keep_hourly = 99" in merged


def test_merge_creates_restic_table_when_input_blank() -> None:
    merged = merge_allow_empty_password_into_backup_toml("", True)
    assert "[restic]" in merged
    assert "allow_empty_password = true" in merged


def test_merge_overwrites_existing_flag() -> None:
    existing = "[restic]\nallow_empty_password = true\n"
    merged = merge_allow_empty_password_into_backup_toml(existing, False)
    assert "allow_empty_password = false" in merged


def test_merge_raises_on_malformed_toml() -> None:
    with pytest.raises(BackupProvisioningError):
        merge_allow_empty_password_into_backup_toml("[snapshot\nbroken", True)


# --- _repository_url_for_bucket ---


def test_repository_url_strips_trailing_slash_and_points_at_bucket_root() -> None:
    assert _repository_url_for_bucket(_ENDPOINT + "/", "u--host-1") == f"s3:{_ENDPOINT}/u--host-1"


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
    # Falls back to info + a freshly minted key rather than erroring out.
    assert cli.minted_key_names == ["host-abc"]
    assert key.secret_access_key.get_secret_value() == "SECRET"


def test_create_or_reuse_propagates_non_exists_errors() -> None:
    cli = _make_cli(create_error_stderr='{"error": "internal error"}')
    with pytest.raises(ImbueCloudCliError):
        _create_or_reuse_bucket(cli, "a@b.com", "host-abc")


# --- _plan_for_request ---


def test_plan_for_configure_later_is_none() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.CONFIGURE_LATER)
    assert _plan_for_request(request, "host-1", imbue_cloud_cli=None) is None


def test_plan_for_imbue_cloud_builds_repo_from_bucket() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.IMBUE_CLOUD,
        master_password=SecretStr("pw"),
        account_email="a@b.com",
    )
    plan = _plan_for_request(request, "host-abc", imbue_cloud_cli=_make_cli())
    assert plan is not None
    assert f"RESTIC_REPOSITORY=s3:{_ENDPOINT}/u--host-abc\n" in plan.restic_env_content
    assert plan.allow_empty_password is False


def test_plan_for_imbue_cloud_requires_cli() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="a@b.com")
    with pytest.raises(BackupProvisioningError):
        _plan_for_request(request, "host-abc", imbue_cloud_cli=None)


def test_plan_for_imbue_cloud_requires_account() -> None:
    request = BackupSetupRequest(backup_provider=BackupProvider.IMBUE_CLOUD, account_email="")
    with pytest.raises(BackupProvisioningError):
        _plan_for_request(request, "host-abc", imbue_cloud_cli=_make_cli())


def test_plan_for_api_key_uses_textarea() -> None:
    request = BackupSetupRequest(
        backup_provider=BackupProvider.API_KEY,
        api_key_env_text="RESTIC_REPOSITORY=s3:r\n",
        master_password=None,
    )
    plan = _plan_for_request(request, "host-abc", imbue_cloud_cli=None)
    assert plan is not None
    assert "RESTIC_REPOSITORY=s3:r" in plan.restic_env_content
    assert plan.allow_empty_password is True
