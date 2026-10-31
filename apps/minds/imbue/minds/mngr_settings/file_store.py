import fcntl
import os
import stat
import time
import tomllib
from collections.abc import Callable
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from typing import Final

import tomlkit
from loguru import logger
from pydantic import Field

from imbue.minds.bootstrap import MindsRoot
from imbue.minds.mngr_settings.interfaces import MindsSettingsStoreInterface
from imbue.minds.mngr_settings.profile_paths import resolve_active_settings_path

# Threshold after which waiting on the write lock is reported as suspicious.
# Writers hold the lock only for a read-mutate-write of a small file, so any real wait indicates a stuck holder.
_LOCK_WAIT_WARN_SECONDS: Final[float] = 2.0

_OWNER_ONLY_MODE: Final[int] = stat.S_IRUSR | stat.S_IWUSR


class FileMindsSettingsStore(MindsSettingsStoreInterface):
    """File-backed settings store that serializes writers via a sidecar flock.

    Concurrent writers exist in practice (signin threads, the startup reconcile, the sync pass and the connection routes materializing provider blocks), so every read-modify-write runs under an exclusive ``flock`` on ``<settings>.lock``.
    Writes go through a tmp-file + rename so readers never see a half-written file, and leave the file
    owner-only: the provider blocks carry cloud credentials in the clear.

    This module is imported by the pre-import reconcile, so it must not import ``imbue.mngr`` (hence
    its own atomic write rather than ``imbue.mngr.utils.file_utils.atomic_write``).
    """

    settings_path: Path = Field(frozen=True, description="The profile's settings.toml")

    def read(self) -> dict[str, Any]:
        if not self.settings_path.exists():
            return {}
        return tomllib.loads(self.settings_path.read_text())

    def update(self, mutate_document: Callable[[tomlkit.TOMLDocument], bool]) -> bool:
        with self._holding_write_lock():
            if self.settings_path.exists():
                doc = tomlkit.loads(self.settings_path.read_text())
            else:
                doc = tomlkit.document()
            is_write_needed = mutate_document(doc)
            if is_write_needed:
                _write_owner_only(self.settings_path, tomlkit.dumps(doc))
            return is_write_needed

    @contextmanager
    def _holding_write_lock(self) -> Iterator[None]:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.settings_path.with_suffix(".lock")
        wait_started_at = time.monotonic()
        with lock_path.open("w") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            waited_seconds = time.monotonic() - wait_started_at
            if waited_seconds > _LOCK_WAIT_WARN_SECONDS:
                logger.warning("Waited {:.1f}s for the settings write lock at {}", waited_seconds, lock_path)
            try:
                yield
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)


def _write_owner_only(path: Path, text: str) -> None:
    """Atomically replace ``path`` with ``text``, created 0600 and left 0600 even when the old file was wider."""
    tmp_path = path.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _OWNER_ONLY_MODE)
    with os.fdopen(fd, "w") as tmp_file:
        tmp_file.write(text)
        tmp_file.flush()
        os.fsync(tmp_file.fileno())
    os.chmod(tmp_path, _OWNER_ONLY_MODE)
    os.replace(tmp_path, path)


def settings_store_for(root: MindsRoot) -> FileMindsSettingsStore | None:
    """Return the store for the env's active profile, or None when the host dir has no active profile dir."""
    settings_path = resolve_active_settings_path(root)
    if settings_path is None:
        return None
    return FileMindsSettingsStore(settings_path=settings_path)
