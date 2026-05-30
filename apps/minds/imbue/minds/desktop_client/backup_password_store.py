"""Persistence for the user's shared restic backup master/recovery password.

One passphrase is shared across all of a user's workspaces. It is
established the first time the user picks master-password encryption (with
the "save this password" box checked) and written, mode 0600, under the
activated minds env's data dir -- ``~/.<minds-env-name>/backup_password``
(``~/.minds/backup_password`` for the default env). On later workspaces
minds reports only that one exists; it never re-displays the value. The
master password never enters a workspace: minds reads the file solely to
``restic init`` each repository and authenticate adding that workspace's own
random key, all from the minds machine. Changing a saved password is
intentionally not handled here (a separate future flow updates it across all
workspaces at once).
"""

import os
from pathlib import Path

from imbue.minds.config.data_types import WorkspacePaths
from imbue.minds.errors import BackupProvisioningError

_BACKUP_PASSWORD_FILENAME = "backup_password"


def backup_password_file_path(paths: WorkspacePaths) -> Path:
    """Return the path of the shared master-password file for this minds env."""
    return paths.data_dir / _BACKUP_PASSWORD_FILENAME


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


def save_backup_password_if_absent(paths: WorkspacePaths, password: str) -> bool:
    """Write the master password the first time only; never overwrite an existing one.

    Returns True if the file was written, False if one already existed.
    """
    path = backup_password_file_path(paths)
    if path.exists():
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file with mode 0600 atomically: O_EXCL guarantees the
        # secret is never visible to other local users (not even in the brief
        # window a write-then-chmod would leave it world-readable), and also
        # closes the TOCTOU gap with the path.exists() check above -- a file
        # that appears in between is treated as "already saved" rather than
        # overwritten.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, password.encode("utf-8"))
        finally:
            os.close(fd)
    except FileExistsError:
        return False
    except OSError as e:
        raise BackupProvisioningError(f"Could not save backup password at {path}: {e}") from e
    return True
