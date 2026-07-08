"""Unit tests for the shared restic backup master-password store (hash + plaintext copy)."""

import stat
from pathlib import Path

from pydantic import SecretStr

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.desktop_client.backup_password_store import backup_password_file_path
from imbue.minds.desktop_client.backup_password_store import backup_password_hash_file_path
from imbue.minds.desktop_client.backup_password_store import delete_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import ensure_backup_password_hash
from imbue.minds.desktop_client.backup_password_store import has_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import read_saved_backup_password
from imbue.minds.desktop_client.backup_password_store import resolve_backup_password_for_use
from imbue.minds.desktop_client.backup_password_store import save_backup_password
from imbue.minds.desktop_client.backup_password_store import verify_backup_password
from imbue.minds.desktop_client.backup_password_store import write_backup_password_hash


def _paths(tmp_path: Path) -> WorkspacePaths:
    return WorkspacePaths(data_dir=tmp_path)


# -- Plaintext convenience copy --


def test_read_returns_none_when_absent(tmp_path: Path) -> None:
    assert read_saved_backup_password(_paths(tmp_path)) is None
    assert has_saved_backup_password(_paths(tmp_path)) is False


def test_save_then_read_round_trips_and_overwrites(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_backup_password(paths, SecretStr("hunter2"))
    assert read_saved_backup_password(paths) == "hunter2"
    assert has_saved_backup_password(paths) is True
    # The plaintext copy is a mirror of a validated value, so re-saving a new
    # (validated) value overwrites it.
    save_backup_password(paths, SecretStr("hunter3"))
    assert read_saved_backup_password(paths) == "hunter3"


def test_delete_saved_backup_password_removes_the_copy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_backup_password(paths, SecretStr("secret"))
    delete_saved_backup_password(paths)
    assert read_saved_backup_password(paths) is None
    # Deleting an absent copy is a no-op.
    delete_saved_backup_password(paths)


def test_saved_files_are_owner_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    save_backup_password(paths, SecretStr("secret"))
    write_backup_password_hash(paths, SecretStr("secret"))
    for path in (backup_password_file_path(paths), backup_password_hash_file_path(paths)):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_read_treats_blank_file_as_unset(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    backup_password_file_path(paths).write_text("   \n")
    assert read_saved_backup_password(paths) is None
    assert has_saved_backup_password(paths) is False


def test_files_live_under_data_dir(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert backup_password_file_path(paths) == tmp_path / "backup_password"
    assert backup_password_hash_file_path(paths) == tmp_path / "backup_password_hash"


# -- Hash seeding + verification --


def test_ensure_seeds_the_empty_password_hash_on_first_start(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    ensure_backup_password_hash(paths)
    assert backup_password_hash_file_path(paths).is_file()
    assert verify_backup_password(paths, SecretStr("")) is True
    assert verify_backup_password(paths, SecretStr("anything")) is False


def test_ensure_seeds_from_an_existing_plaintext_copy(tmp_path: Path) -> None:
    # A pre-hash install has only the plaintext file; the seeded hash must
    # accept that password (and reject the empty one).
    paths = _paths(tmp_path)
    save_backup_password(paths, SecretStr("legacy-password"))
    ensure_backup_password_hash(paths)
    assert verify_backup_password(paths, SecretStr("legacy-password")) is True
    assert verify_backup_password(paths, SecretStr("")) is False


def test_ensure_never_overwrites_an_existing_hash(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("established"))
    save_backup_password(paths, SecretStr("a-different-plaintext"))
    ensure_backup_password_hash(paths)
    assert verify_backup_password(paths, SecretStr("established")) is True


def test_write_hash_stores_no_plaintext(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("visible-nowhere"))
    hash_content = backup_password_hash_file_path(paths).read_text()
    assert "visible-nowhere" not in hash_content
    assert hash_content.startswith("$argon2")


# -- Resolution for repo-initializing flows --


def test_resolve_blank_means_the_empty_password_when_nothing_is_saved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    resolved, error = resolve_backup_password_for_use(paths, SecretStr(""))
    assert error is None
    assert resolved is not None and resolved.get_secret_value() == ""


def test_resolve_blank_falls_back_to_the_saved_copy(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("shared-secret"))
    save_backup_password(paths, SecretStr("shared-secret"))
    resolved, error = resolve_backup_password_for_use(paths, SecretStr(""))
    assert error is None
    assert resolved is not None and resolved.get_secret_value() == "shared-secret"


def test_resolve_rejects_a_wrong_typed_password(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    resolved, error = resolve_backup_password_for_use(paths, SecretStr("wrong"))
    assert resolved is None
    assert error is not None and "incorrect" in error


def test_resolve_rejects_blank_when_a_password_is_set_and_nothing_is_saved(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    resolved, error = resolve_backup_password_for_use(paths, SecretStr(""))
    assert resolved is None
    assert error is not None and "master password" in error


def test_resolve_rejects_a_stale_saved_copy(tmp_path: Path) -> None:
    # The hash rotated but a stale plaintext copy survived: blank must not
    # silently use the stale value.
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("new-password"))
    save_backup_password(paths, SecretStr("old-password"))
    resolved, error = resolve_backup_password_for_use(paths, SecretStr(""))
    assert resolved is None
    assert error is not None and "saved" in error


def test_resolve_accepts_a_correct_typed_password(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    write_backup_password_hash(paths, SecretStr("right"))
    resolved, error = resolve_backup_password_for_use(paths, SecretStr("right"))
    assert error is None
    assert resolved is not None and resolved.get_secret_value() == "right"
