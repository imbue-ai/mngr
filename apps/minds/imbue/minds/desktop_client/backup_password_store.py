"""Persistence for the user's shared restic backup master/recovery password.

One passphrase is shared across all of a user's workspaces. Two files live
under the activated minds env's data dir (``~/.minds/`` for the default env):

* ``backup_password_hash`` -- an argon2 hash of the master password. Always
  present (seeded at app startup: from the plaintext file when one exists,
  else the hash of the empty string) and the single validation authority:
  any flow that needs the master password verifies the candidate against it.
  The application therefore starts in the "empty master password" state and
  a new user can create workspaces without ever typing one.
* ``backup_password`` -- the optional plaintext convenience copy, so the user
  does not have to retype the password on every repo-initializing flow. It
  can only ever be (re)written with a value that was just validated against
  the hash; establishing or *changing* the password is exclusively the
  Settings-page rotation flow's job.

The master password never enters a workspace: minds uses it solely to
``restic init`` each repository and authenticate key operations, all from
the minds machine.
"""

import os
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError
from argon2.exceptions import VerificationError
from argon2.exceptions import VerifyMismatchError
from pydantic import SecretStr

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import BackupProvisioningError

_BACKUP_PASSWORD_FILENAME = "backup_password"
_BACKUP_PASSWORD_HASH_FILENAME = "backup_password_hash"

_PASSWORD_HASHER = PasswordHasher()


def backup_password_file_path(paths: WorkspacePaths) -> Path:
    """Return the path of the shared master-password file for this minds env."""
    return paths.data_dir / _BACKUP_PASSWORD_FILENAME


def backup_password_hash_file_path(paths: WorkspacePaths) -> Path:
    """Return the path of the master-password hash file for this minds env."""
    return paths.data_dir / _BACKUP_PASSWORD_HASH_FILENAME


def has_saved_backup_password(paths: WorkspacePaths) -> bool:
    """Return whether a non-empty master password has already been saved."""
    return read_saved_backup_password(paths) is not None


def read_saved_backup_password(paths: WorkspacePaths) -> str | None:
    """Return the saved master password, or None if none has been saved yet."""
    path = backup_password_file_path(paths)
    if not path.is_file():
        return None
    try:
        content = path.read_text()
    except OSError as e:
        raise BackupProvisioningError(f"Could not read saved backup password at {path}: {e}") from e
    stripped = content.strip()
    return stripped or None


def _write_secret_file(path: Path, content: str) -> None:
    """Atomically write a 0600 secret file (temp file + rename, never world-readable)."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
        finally:
            os.close(fd)
        tmp_path.rename(path)
    except OSError as e:
        raise BackupProvisioningError(f"Could not write {path}: {e}") from e


def save_backup_password(paths: WorkspacePaths, password: SecretStr) -> None:
    """(Over)write the plaintext convenience copy of the master password.

    Callers must only pass a value already validated against the hash (or the
    value the hash was just rotated to) -- this file is a convenience mirror,
    never an authority.
    """
    _write_secret_file(backup_password_file_path(paths), password.get_secret_value())


def delete_saved_backup_password(paths: WorkspacePaths) -> None:
    """Remove the plaintext convenience copy (e.g. it went stale on rotation)."""
    try:
        backup_password_file_path(paths).unlink(missing_ok=True)
    except OSError as e:
        raise BackupProvisioningError(f"Could not delete the saved backup password: {e}") from e


def write_backup_password_hash(paths: WorkspacePaths, password: SecretStr) -> None:
    """Hash the password (argon2) and persist it as the validation authority."""
    _write_secret_file(backup_password_hash_file_path(paths), _PASSWORD_HASHER.hash(password.get_secret_value()))


def ensure_backup_password_hash(paths: WorkspacePaths) -> None:
    """Seed ``backup_password_hash`` if it does not exist yet (app startup).

    Seeded from the plaintext convenience copy when one exists (pre-hash
    installs keep working unchanged), else from the empty string -- the
    application's initial "no master password" state.
    """
    if backup_password_hash_file_path(paths).is_file():
        return
    saved = read_saved_backup_password(paths)
    write_backup_password_hash(paths, SecretStr(saved if saved is not None else ""))


def verify_backup_password(paths: WorkspacePaths, candidate: SecretStr) -> bool:
    """Return whether ``candidate`` matches the stored master-password hash.

    Self-healing: seeds the hash file first if it is missing, so callers can
    rely on a verdict even before the startup hook ran (e.g. in tests).
    """
    ensure_backup_password_hash(paths)
    path = backup_password_hash_file_path(paths)
    try:
        stored_hash = path.read_text().strip()
    except OSError as e:
        raise BackupProvisioningError(f"Could not read the backup password hash at {path}: {e}") from e
    try:
        _PASSWORD_HASHER.verify(stored_hash, candidate.get_secret_value())
    except (VerifyMismatchError, VerificationError):
        return False
    except InvalidHashError as e:
        raise BackupProvisioningError(f"The backup password hash at {path} is not a valid argon2 hash") from e
    return True


def is_master_password_set(paths: WorkspacePaths) -> bool:
    """Return whether the master password is currently non-empty.

    Drives which forms need a password input at all: while the hash is still
    the empty-password seed, no flow ever needs the user to type anything.
    """
    return not verify_backup_password(paths, SecretStr(""))


def resolve_backup_password_for_use(
    paths: WorkspacePaths, typed_password: SecretStr
) -> tuple[SecretStr | None, str | None]:
    """Resolve the master password for a repo-initializing flow.

    A non-blank typed value must match the hash. A blank value falls back to
    the saved plaintext copy when one exists, else means the empty password --
    both also validated against the hash. Returns ``(password, None)`` on
    success or ``(None, user-facing error message)`` on a mismatch.
    """
    typed_value = typed_password.get_secret_value()
    if typed_value:
        if not verify_backup_password(paths, typed_password):
            return None, "The backup master password is incorrect."
        return typed_password, None
    saved = read_saved_backup_password(paths)
    if saved is not None:
        if not verify_backup_password(paths, SecretStr(saved)):
            return None, (
                "The saved backup password no longer matches the current master password; "
                "enter the master password explicitly."
            )
        return SecretStr(saved), None
    if not verify_backup_password(paths, SecretStr("")):
        return None, "A backup master password is set; enter it to continue."
    return SecretStr(""), None
