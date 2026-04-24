"""Thread-safe JSONL writer for iframe console logs received from the Electron client.

The Electron desktop client forwards renderer-side console messages from
agent-owned service iframes (``/service/<name>/``) to the workspace server
via ``POST /api/iframe-logs``. This module owns the file handle and rotation
for ``<host_dir>/logs/iframe/events.jsonl`` where those records land.
"""

import json
import threading
import uuid
from datetime import datetime
from datetime import timezone
from itertools import count
from pathlib import Path
from typing import Any
from typing import IO

from pydantic import ConfigDict
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.mutable_model import MutableModel

_DEFAULT_MAX_BYTES = 10 * 1024 * 1024


def _now_iso() -> str:
    """Return an ISO-8601 UTC timestamp with nanosecond precision padding.

    Matches the format used by ``imbue_common.logging._build_flat_log_dict``
    so all workspace-owned JSONL files share a timestamp shape.
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

    Rotation matches the convention in ``imbue_common.logging.make_jsonl_file_sink``:
    when the active file exceeds ``max_size_bytes``, it is renamed to
    ``<name>.1`` (or the next free numeric suffix) and a fresh file is opened.
    Safe to share across threads; each call serializes writes under an
    internal lock.
    """

    model_config = ConfigDict(
        frozen=False,
        extra="forbid",
        arbitrary_types_allowed=True,
    )

    file_path: Path = Field(description="Active log file; rotated siblings use numeric suffixes")
    max_size_bytes: int = Field(
        default=_DEFAULT_MAX_BYTES,
        description="Rotate the active file when this threshold is crossed",
    )

    _fh: IO[str] | None = PrivateAttr(default=None)
    _size: int = PrivateAttr(default=0)
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
            self._fh = self.file_path.open("a")
            try:
                self._size = self.file_path.stat().st_size
            except OSError:
                self._size = 0
        return self._fh

    def _rotate_if_needed_locked(self) -> None:
        if self._size < self.max_size_bytes:
            return
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        for index in count(1):
            candidate = self.file_path.with_name(f"{self.file_path.name}.{index}")
            if not candidate.exists():
                self.file_path.rename(candidate)
                break
        self._size = 0
