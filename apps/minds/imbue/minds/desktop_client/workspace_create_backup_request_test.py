"""Unit tests for ``build_backup_request_or_error`` (form/API backup resolution)."""

from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.workspace_create import build_backup_request_or_error
from imbue.minds.primitives import BackupProvider


def _build(
    backup_provider: BackupProvider,
    api_key_env: str = "",
    account_email: str = "",
) -> tuple[BackupSetupRequest | None, str | None]:
    return build_backup_request_or_error(
        backup_provider=backup_provider,
        api_key_env=api_key_env,
        account_email=account_email,
    )


def test_configure_later_yields_request_without_error() -> None:
    request, error = _build(BackupProvider.CONFIGURE_LATER)
    assert error is None
    assert request is not None and request.backup_provider is BackupProvider.CONFIGURE_LATER


def test_imbue_cloud_without_account_is_an_error() -> None:
    request, error = _build(BackupProvider.IMBUE_CLOUD, account_email="")
    assert request is None
    assert error is not None and "account" in error.lower()


def test_imbue_cloud_with_account_builds_a_request() -> None:
    request, error = _build(BackupProvider.IMBUE_CLOUD, account_email="alice@example.com")
    assert error is None
    assert request is not None
    assert request.backup_provider is BackupProvider.IMBUE_CLOUD
    assert request.account_email == "alice@example.com"
    # The api_key env block is only carried for API_KEY backups.
    assert request.api_key_env_text == ""


def test_api_key_env_rejects_a_user_supplied_restic_password() -> None:
    request, error = _build(
        BackupProvider.API_KEY,
        api_key_env="RESTIC_REPOSITORY=s3:x\nRESTIC_PASSWORD=nope\n",
    )
    assert request is None
    assert error is not None and "RESTIC_PASSWORD" in error


def test_api_key_env_is_carried_verbatim() -> None:
    env_text = "RESTIC_REPOSITORY=s3:https://r2.example/bucket\nAWS_ACCESS_KEY_ID=ak\nAWS_SECRET_ACCESS_KEY=sk\n"
    request, error = _build(BackupProvider.API_KEY, api_key_env=env_text)
    assert error is None
    assert request is not None
    assert request.api_key_env_text == env_text
