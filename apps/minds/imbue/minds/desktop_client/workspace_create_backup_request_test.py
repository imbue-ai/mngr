"""Unit tests for ``build_backup_request_or_error`` (form/API backup resolution)."""

from pathlib import Path

from pydantic import SecretStr

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import save_backup_password
from imbue.minds.desktop_client.backup_password_store import write_backup_password_hash
from imbue.minds.desktop_client.backup_provisioning import BackupSetupRequest
from imbue.minds.desktop_client.workspace_create import build_backup_request_or_error
from imbue.minds.primitives import BackupProvider


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def _build(
    tmp_path: Path,
    *,
    backup_provider: BackupProvider = BackupProvider.CONFIGURE_LATER,
    typed_master_password: str = "",
    is_save_password: bool = False,
    api_key_env: str = "",
    account_email: str = "",
) -> tuple[BackupSetupRequest | None, str | None]:
    return build_backup_request_or_error(
        backup_provider=backup_provider,
        typed_master_password=SecretStr(typed_master_password),
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


def test_blank_password_on_a_fresh_install_means_the_empty_password(tmp_path: Path) -> None:
    # The app starts with the empty-password hash, so a fresh user can create
    # a backed-up workspace without ever typing a master password.
    request, error = _build(tmp_path, backup_provider=BackupProvider.API_KEY, api_key_env="RESTIC_REPOSITORY=s3:r\n")
    assert error is None
    assert request is not None and request.master_password is None


def test_wrong_typed_password_is_an_error(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr("wrong"),
        is_save_password=False,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert request is None
    assert error is not None and "incorrect" in error


def test_blank_password_is_an_error_once_a_real_password_is_set(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr(""),
        is_save_password=False,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert request is None
    assert error is not None


def test_correct_typed_password_is_used_and_optionally_saved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("topsecret"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr("topsecret"),
        is_save_password=True,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None
    assert request.master_password is not None
    assert request.master_password.get_secret_value() == "topsecret"
    # The save box was checked and the value validated, so it is now on disk.
    assert read_saved_backup_password(paths) == "topsecret"


def test_wrong_typed_password_is_never_saved(tmp_path: Path) -> None:
    # save_password only persists a value that validated against the hash --
    # it can never establish or change the master password.
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr("wrong"),
        is_save_password=True,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert request is None
    assert error is not None
    assert read_saved_backup_password(paths) is None


def test_correct_typed_password_without_save_is_not_persisted(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("ephemeral"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr("ephemeral"),
        is_save_password=False,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None and request.master_password is not None
    assert read_saved_backup_password(paths) is None


def test_blank_password_uses_the_saved_copy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("original"))
    save_backup_password(paths, SecretStr("original"))
    request, error = build_backup_request_or_error(
        backup_provider=BackupProvider.API_KEY,
        typed_master_password=SecretStr(""),
        is_save_password=False,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
        account_email="",
        paths=paths,
    )
    assert error is None
    assert request is not None and request.master_password is not None
    assert request.master_password.get_secret_value() == "original"


def test_api_key_rejects_restic_password_in_textarea(tmp_path: Path) -> None:
    # The user may not set RESTIC_PASSWORD: minds assigns each workspace its
    # own random repository password, so a textarea password is an error.
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.API_KEY,
        api_key_env="RESTIC_REPOSITORY=s3:r\nRESTIC_PASSWORD=nope\n",
    )
    assert request is None
    assert error is not None and "RESTIC_PASSWORD" in error


def test_api_key_env_text_is_carried_through(tmp_path: Path) -> None:
    request, error = _build(
        tmp_path,
        backup_provider=BackupProvider.API_KEY,
        api_key_env="RESTIC_REPOSITORY=s3:r\n",
    )
    assert error is None
    assert request is not None
    assert request.api_key_env_text == "RESTIC_REPOSITORY=s3:r\n"
