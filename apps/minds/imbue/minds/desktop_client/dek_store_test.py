import stat
from pathlib import Path
from uuid import uuid4

import pytest
from argon2 import PasswordHasher
from pydantic import SecretStr

from imbue.imbue_common.secret_wrapping import SecretWrappingError
from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.dek_store import bundle_mirror_path
from imbue.minds.desktop_client.dek_store import convert_legacy_password_files
from imbue.minds.desktop_client.dek_store import dek_file_path
from imbue.minds.desktop_client.dek_store import ensure_dek
from imbue.minds.desktop_client.dek_store import is_account_unlocked
from imbue.minds.desktop_client.dek_store import is_master_password_set_for_account
from imbue.minds.desktop_client.dek_store import load_dek
from imbue.minds.desktop_client.dek_store import read_bundle_mirror
from imbue.minds.desktop_client.dek_store import set_master_password_for_account
from imbue.minds.desktop_client.dek_store import unlock_account_with_bundle
from imbue.minds.desktop_client.dek_store import unwrap_bundle_json
from imbue.minds.desktop_client.dek_store import verify_master_password_for_account


@pytest.fixture
def paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


def _user_id() -> str:
    return uuid4().hex


def test_ensure_dek_creates_a_0600_file_and_is_stable(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    dek = ensure_dek(paths, user_id)

    assert len(dek) == 32
    assert ensure_dek(paths, user_id) == dek
    assert load_dek(paths, user_id) == dek
    mode = stat.S_IMODE(dek_file_path(paths, user_id).stat().st_mode)
    assert mode == 0o600
    assert is_account_unlocked(paths, user_id)


def test_load_dek_returns_none_when_locked(paths: WorkspacePaths) -> None:
    assert load_dek(paths, _user_id()) is None
    assert not is_account_unlocked(paths, _user_id())


def test_set_master_password_writes_bundle_and_verification_works(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    bundle = set_master_password_for_account(paths, user_id, SecretStr("hunter2"))

    assert bundle is not None
    assert is_master_password_set_for_account(paths, user_id)
    assert verify_master_password_for_account(paths, user_id, SecretStr("hunter2"))
    assert not verify_master_password_for_account(paths, user_id, SecretStr("wrong"))
    assert not verify_master_password_for_account(paths, user_id, SecretStr(""))
    # The bundle unwraps back to the on-disk DEK.
    assert unwrap_bundle_json(bundle, SecretStr("hunter2")) == load_dek(paths, user_id)


def test_empty_password_state_verifies_only_the_empty_password(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    ensure_dek(paths, user_id)

    assert not is_master_password_set_for_account(paths, user_id)
    assert verify_master_password_for_account(paths, user_id, SecretStr(""))
    assert not verify_master_password_for_account(paths, user_id, SecretStr("anything"))


def test_clearing_the_password_deletes_the_bundle_mirror(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    set_master_password_for_account(paths, user_id, SecretStr("hunter2"))

    result = set_master_password_for_account(paths, user_id, SecretStr(""))

    assert result is None
    assert not is_master_password_set_for_account(paths, user_id)
    # The DEK itself is untouched by a password change.
    assert load_dek(paths, user_id) is not None


def test_password_change_rewraps_without_changing_the_dek(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    first_bundle = set_master_password_for_account(paths, user_id, SecretStr("old"))
    dek_before = load_dek(paths, user_id)

    second_bundle = set_master_password_for_account(paths, user_id, SecretStr("new"))

    assert load_dek(paths, user_id) == dek_before
    assert second_bundle is not None and first_bundle is not None
    assert second_bundle["key_epoch"] == first_bundle["key_epoch"]
    assert unwrap_bundle_json(second_bundle, SecretStr("new")) == dek_before
    with pytest.raises(SecretWrappingError):
        unwrap_bundle_json(second_bundle, SecretStr("old"))


def test_unlock_account_with_bundle_installs_dek_and_mirror(paths: WorkspacePaths) -> None:
    # Simulate device A wrapping, then device B (fresh paths) unlocking.
    user_id = _user_id()
    device_a = WorkspacePaths(data_dir=paths.data_dir / "device-a")
    dek = ensure_dek(device_a, user_id)
    bundle = set_master_password_for_account(device_a, user_id, SecretStr("hunter2"))
    assert bundle is not None

    device_b = WorkspacePaths(data_dir=paths.data_dir / "device-b")
    with pytest.raises(SecretWrappingError):
        unlock_account_with_bundle(device_b, user_id, bundle, SecretStr("wrong"))
    unlocked = unlock_account_with_bundle(device_b, user_id, bundle, SecretStr("hunter2"))

    assert unlocked == dek
    assert load_dek(device_b, user_id) == dek
    assert read_bundle_mirror(device_b, user_id) == bundle


def test_convert_legacy_files_carries_over_a_saved_password(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    (paths.data_dir / "backup_password").write_text("legacy-pass\n")
    (paths.data_dir / "backup_password_hash").write_text(PasswordHasher().hash("legacy-pass"))

    convert_legacy_password_files(paths, [user_id])

    assert is_master_password_set_for_account(paths, user_id)
    assert verify_master_password_for_account(paths, user_id, SecretStr("legacy-pass"))
    assert not (paths.data_dir / "backup_password").exists()
    assert not (paths.data_dir / "backup_password_hash").exists()
    assert (paths.data_dir / "backup_password.pre-sync").exists()
    assert (paths.data_dir / "backup_password_hash.pre-sync").exists()


def test_convert_legacy_files_with_empty_password_seed_sets_no_password(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    (paths.data_dir / "backup_password_hash").write_text(PasswordHasher().hash(""))

    convert_legacy_password_files(paths, [user_id])

    assert is_account_unlocked(paths, user_id)
    assert not is_master_password_set_for_account(paths, user_id)
    assert not (paths.data_dir / "backup_password_hash").exists()


def test_convert_legacy_files_with_unknowable_password_starts_fresh(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    (paths.data_dir / "backup_password_hash").write_text(PasswordHasher().hash("forgotten"))

    convert_legacy_password_files(paths, [user_id])

    assert is_account_unlocked(paths, user_id)
    assert not is_master_password_set_for_account(paths, user_id)
    assert (paths.data_dir / "backup_password_hash.pre-sync").exists()


def test_convert_legacy_files_is_a_no_op_without_accounts(paths: WorkspacePaths) -> None:
    (paths.data_dir / "backup_password_hash").write_text(PasswordHasher().hash("keep-me"))

    convert_legacy_password_files(paths, [])

    # Files stay for the first signed-in run to convert.
    assert (paths.data_dir / "backup_password_hash").exists()


def test_convert_legacy_files_is_idempotent(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    (paths.data_dir / "backup_password").write_text("legacy-pass\n")
    (paths.data_dir / "backup_password_hash").write_text(PasswordHasher().hash("legacy-pass"))

    convert_legacy_password_files(paths, [user_id])
    bundle_after_first = read_bundle_mirror(paths, user_id)
    convert_legacy_password_files(paths, [user_id])

    assert read_bundle_mirror(paths, user_id) == bundle_after_first


def test_corrupt_bundle_mirror_is_treated_as_no_password(paths: WorkspacePaths) -> None:
    user_id = _user_id()
    ensure_dek(paths, user_id)
    bundle_mirror_path(paths, user_id).parent.mkdir(parents=True, exist_ok=True)
    bundle_mirror_path(paths, user_id).write_text("{not json")

    assert read_bundle_mirror(paths, user_id) is None
    assert not verify_master_password_for_account(paths, user_id, SecretStr("x"))
    assert verify_master_password_for_account(paths, user_id, SecretStr(""))
