"""Unit tests for ``_build_backup_request_or_error`` (form/API backup resolution)."""

from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.app import _build_backup_request_or_error
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.primitives import BackupEncryptionMethod
from imbue.minds.primitives import BackupProvider


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def _build(
    tmp_path: Path,
    *,
    backup_provider: BackupProvider = BackupProvider.CONFIGURE_LATER,
    encryption_method: BackupEncryptionMethod = BackupEncryptionMethod.NO_PASSWORD,
    typed_master_password: str = "",
    is_save_password: bool = False,
    api_key_env: str = "",
    account_email: str = "",
) -> tuple[BackupSetupRequest | None, str | None]:
    return _build_backup_request_or_error(
        backup_provider=backup_provider,
        encryption_method=encryption_method,
        typed_master_password=typed_master_password,
        is_save_password=is_save_password,
        api_key_env=api_key_env,
        account_email=account_email,
        paths=_paths(tmp_path),
    )


def test_configure_later_yields_request_without_error(tmp_path: Path) -> None:
    request, error = _build(tmp_path, backup_provider=BackupProvider.CONFIGURE_LATER)
    assert error is None
    assert request is not None and request.backup_provider is BackupProvider.CONFIGURE_LATER


def test_imbue_cloud_without_account_is_an_error(tmp_path: Path) -> None:
    request, error = _build(tmp_path, backup_provider=BackupProvider.IMBUE_CLOUD, account_email="")
    assert request is None
    assert error is not None and "account" in error.lower()


def test_master_password_without_typed_or_saved_is_an_error(tmp_path: Path) -> None:
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
        typed_master_password="",
    )
    assert request is None
    assert error is not None


def test_master_password_typed_and_saved_persists_and_is_used(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    request, error = _build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
        typed_master_password="topsecret",
        is_save_password=True,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None
    assert request.master_password is not None
    assert request.master_password.get_secret_value() == "topsecret"
    # The save box was checked, so the passphrase is now on disk.
    assert read_saved_backup_password(paths) == "topsecret"


def test_master_password_typed_without_save_is_not_persisted(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    request, error = _build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
        typed_master_password="ephemeral",
        is_save_password=False,
        api_key_env="",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None and request.master_password is not None
    assert request.master_password.get_secret_value() == "ephemeral"
    assert read_saved_backup_password(paths) is None


def test_master_password_uses_saved_over_typed(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    # Establish a saved password first.
    _build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
        typed_master_password="original",
        is_save_password=True,
        api_key_env="",
        account_email="",
        paths=paths,
    )
    request, error = _build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.MASTER_PASSWORD,
        typed_master_password="a-different-typed-value",
        is_save_password=False,
        api_key_env="",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None and request.master_password is not None
    assert request.master_password.get_secret_value() == "original"


def test_no_password_leaves_master_password_unset(tmp_path: Path) -> None:
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.IMBUE_CLOUD,
        encryption_method=BackupEncryptionMethod.NO_PASSWORD,
        account_email="a@b.com",
    )
    assert error is None
    assert request is not None and request.master_password is None


def test_api_key_rejects_restic_password_in_textarea(tmp_path: Path) -> None:
    # The user may not set RESTIC_PASSWORD: minds assigns each workspace its
    # own random repository password, so a textarea password is an error.
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.NO_PASSWORD,
        api_key_env="RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=nope\n",
    )
    assert request is None
    assert error is not None and "RESTIC_PASSWORD" in error


def test_api_key_env_text_is_carried_through(tmp_path: Path) -> None:
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.API_KEY,
        encryption_method=BackupEncryptionMethod.NO_PASSWORD,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
    )
    assert error is None
    assert request is not None
    assert request.api_key_env_text == "RESTIC_REPOSITORY=s3:r\n"
