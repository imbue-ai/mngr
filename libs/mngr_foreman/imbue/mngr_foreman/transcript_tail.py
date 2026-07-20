"""Byte-offset follower for a Claude agent's mirrored transcript JSONL.

mngr mirrors every claude agent's raw session JSONL, verbatim and untruncated,
to ``<host_dir>/agents/<id>/logs/claude_transcript/events.jsonl`` on the agent's
host (provisioned unconditionally by ``mngr_claude``). We read it through an
:class:`~imbue.mngr.api.events.EventsTarget`'s ``HostFileReadInterface`` -- whose
``read_file`` is byte-exact (local direct / remote SFTP) -- and follow it the
same way ``mngr``'s own remote event follower does: whole-file read, advance a
byte offset over complete lines only, hold back a trailing partial line, and
reset the offset when the file shrinks (rotation).

The tailer itself is deliberately I/O-agnostic: it is driven by a ``reader``
callable returning the file's current bytes, so it can be unit-tested against a
fake ``read_file`` (rotation, partial lines) with no host. The server supplies a
reader that reads through the resolved ``EventsTarget`` and periodically
refreshes it (``refresh_events_target``) to survive host stop/start.
"""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger

from imbue.mngr.utils.jsonl_warn import split_complete_lines

# Relative path of the mirrored raw-transcript file under an agent's state dir,
# reached from the agent's ``events`` dir (``events_path``) via its parent.
TRANSCRIPT_SUBPATH = "logs/claude_transcript/events.jsonl"

# Sentinel a reader raises/returns for "file not there yet" -- treated as empty.
ReaderFn = Callable[[], bytes]


class TranscriptTailer:
    """Stateful follower over one transcript file, driven by a ``reader``.

    Call :meth:`poll` repeatedly; each call returns the list of newly completed
    (newline-terminated) lines since the previous call, in order. A trailing
    partial line is never returned until its newline arrives on a later poll.
    """

    def __init__(self, reader: ReaderFn) -> None:
        self._reader = reader
        self._byte_offset = 0

    @property
    def byte_offset(self) -> int:
        return self._byte_offset

    def poll(self) -> list[str]:
        """Read the current file and return any newly completed lines.

        On any read error returns ``[]`` without advancing the offset, so the
        next poll retries from the same position (matching mngr's follower,
        which tolerates a transiently-unreadable remote file).
        """
        try:
            content_bytes = self._reader()
        except (FileNotFoundError, OSError) as e:
            logger.trace("Transcript read failed (will retry): {}", e)
            return []

        current_length = len(content_bytes)

        # File shrank => it was rotated/truncated. Re-read from the start; the
        # parser dedups by event_id so re-reading old lines never double-emits.
        if current_length < self._byte_offset:
            logger.debug("Transcript file shrank ({} < {}); treating as rotation", current_length, self._byte_offset)
            self._byte_offset = 0

        if current_length <= self._byte_offset:
            return []

        new_content = content_bytes[self._byte_offset :].decode("utf-8", errors="replace")
        # Consume only up to the last newline; leave any trailing partial write
        # for the next poll so a mid-flush line is not split and lost.
        lines, bytes_consumed = split_complete_lines(new_content)
        self._byte_offset += bytes_consumed
        return lines
