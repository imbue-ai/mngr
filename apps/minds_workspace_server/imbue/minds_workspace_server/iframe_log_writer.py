"""Thread-safe JSONL writer for iframe console logs received from the Electron client.

The Electron desktop client forwards renderer-side console messages from
agent-owned service iframes (``/service/<name>/``) to the workspace server
via ``POST /api/iframe-logs``. This module owns the file handle and rotation
for ``<host_dir>/logs/iframe/events.jsonl`` where those records land.

Rotation, cross-process locking, and retention are delegated to the helpers
in ``imbue_common.logging`` so this file stays consistent with every other
``events.jsonl`` stream in the codebase (same ``.<timestamp>`` suffix shape,
same retention cap, same ``fcntl`` lock).
"""

import json
import threading
import uuid
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import IO

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.logging import cleanup_old_rotated_files
from imbue.imbue_common.logging import generate_rotation_timestamp
from imbue.imbue_common.logging import rotation_lock
from imbue.imbue_common.mutable_model import MutableModel

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_ROTATED_COUNT = 10


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with a 9-digit fractional-second field.

    Python ``datetime`` resolves only to microseconds; the trailing ``000`` in
    the format string pads the microsecond field (``%f``, 6 digits) to the
    nine-digit fractional-second shape used by
    ``imbue_common.logging.format_nanosecond_iso_timestamp`` so all
    workspace-owned JSONL files share the same timestamp column width.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f000Z")


def _new_event_id() -> str:
    return f"evt-{uuid.uuid4().hex}"


def _build_envelope(record: dict[str, Any], *, timestamp: str) -> dict[str, Any]:
    """Wrap a caller-supplied record in the standard event envelope.

    The ``source`` field is built from the record's ``service_name`` and
    ``mind_id`` so each line is self-describing without needing the filename.
    """
    service_name = str(record.get("service_name", "unknown"))
    mind_id = str(record.get("mind_id", "unknown"))
    envelope: dict[str, Any] = {
        "timestamp": timestamp,
        "type": "electron",
        "event_id": _new_event_id(),
        "source": f"electron/renderer/service/{service_name}/{mind_id}",
    }
    envelope.update(record)
    return envelope


class IframeLogWriter(MutableModel):
    """Thread-safe appender for iframe-log JSONL with size-based rotation.

    Rotation uses the shared ``imbue_common.logging`` helpers: when the active
    file exceeds ``max_size_bytes``, it is renamed to ``<name>.<timestamp>``
    under a cross-process ``fcntl`` lock, and the oldest rotated siblings
    beyond ``max_rotated_count`` are deleted.

    Safe to share across threads; each ``write_records`` call serializes
    writes under an internal lock.
    """

    model_config = ConfigDict(
        frozen=False,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    file_path: Path = Field(description="Active log file; rotated siblings are named <file>.<timestamp>")
    max_size_bytes: int = Field(
        default=_DEFAULT_MAX_BYTES,
        description="Rotate the active file when this threshold is crossed",
    )
    max_rotated_count: int = Field(
        default=_DEFAULT_MAX_ROTATED_COUNT,
        description="Maximum number of rotated files to keep; oldest are pruned on rotation",
    )

    _fh: IO[str] | None = PrivateAttr(default=None)
    _size: int = PrivateAttr(default=0)
    _cleaned_up: bool = PrivateAttr(default=False)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def write_records(self, records: list[dict[str, Any]], *, now_iso: str | None = None) -> int:
        """Append each record as a single JSONL line; returns the number written.

        The caller supplies per-record fields (level, message, frame_url, etc.).
        The writer adds the envelope fields (``timestamp``, ``event_id``,
        ``type``, ``source``) and serializes. ``now_iso`` is exposed for tests
        that need deterministic timestamps; production callers pass ``None``.
        """
        if not records:
            return 0
        timestamp = now_iso if now_iso is not None else _now_iso()
        written = 0
        with self._lock:
            for record in records:
                envelope = _build_envelope(record, timestamp=timestamp)
                line = json.dumps(envelope, separators=(",", ":"), default=str) + "\n"
                line_bytes = len(line.encode("utf-8"))
                # Open first so ``self._size`` reflects the on-disk size before
                # the rotation check. Otherwise the first write after startup
                # would observe the default ``_size = 0`` and skip rotation
                # even when the existing file is already past ``max_size_bytes``
                # (e.g. restart after a crash that left an oversized log).
                self._ensure_open_locked()
                self._rotate_if_needed_locked()
                fh = self._ensure_open_locked()
                fh.write(line)
                fh.flush()
                self._size += line_bytes
                written += 1
        return written

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                self._fh.close()
                self._fh = None

    def _ensure_open_locked(self) -> IO[str]:
        if self._fh is None:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            # Prune old rotated files on first open, matching the one-shot
            # cleanup in imbue_common.logging.make_jsonl_file_sink so crash
            # recovery doesn't leave an unbounded rotation history.
            if not self._cleaned_up:
                cleanup_old_rotated_files(self.file_path.parent, self.max_rotated_count)
                self._cleaned_up = True
            self._fh = self.file_path.open("a")
            try:
                self._size = self.file_path.stat().st_size
            except OSError:
                self._size = 0
        return self._fh

    def _rotate_if_needed_locked(self) -> None:
        if self._size < self.max_size_bytes:
            return
        with rotation_lock(self.file_path.parent):
            # Another process may have already rotated while we waited for the
            # lock; re-check on-disk size and either reopen or proceed.
            try:
                actual_size = self.file_path.stat().st_size
            except OSError:
                actual_size = 0
            if actual_size < self.max_size_bytes:
                if self._fh is not None:
                    self._fh.close()
                    self._fh = None
                self._size = actual_size
                return
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            timestamp = generate_rotation_timestamp()
            rotated = self.file_path.with_name(f"{self.file_path.name}.{timestamp}")
            self.file_path.rename(rotated)
            cleanup_old_rotated_files(self.file_path.parent, self.max_rotated_count)
            self._size = 0
