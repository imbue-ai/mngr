"""Bounded, cached reads of the transcripts that compaction eligibility checks parse.

A compaction harness decides whether an agent is due for compaction from two values in its
append-only transcript: when the newest assistant turn happened and how many prompt tokens it
used. A compaction record newer than every token count leaves the count unknown until the next
turn reports one. Transcripts grow to tens of megabytes and the autocompact sweep checks every
opted-in agent once a minute, so a transcript is read backward from its end only until both
values are found, and the result is cached so a later check reads only the bytes appended since.

``COMPACTION_TRANSCRIPT_CACHE`` is module-level state because nothing else outlives a sweep: a
long-lived process that embeds mngr builds a fresh MngrContext and fresh agent objects for every
sweep, so a cache held by either would start empty each time. The sweep evaluates agents
concurrently, so the cache is guarded by a lock; it is bounded by evicting the entries of agents a
sweep no longer evaluates and by a hard cap on its size.
"""

import threading
from abc import ABC
from collections.abc import Callable
from collections.abc import Set as AbstractSet
from datetime import datetime
from pathlib import Path
from typing import Final

from loguru import logger
from pydantic import Field
from pydantic import PrivateAttr

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.imbue_common.mutable_model import MutableModel
from imbue.mngr.agents.jsonl_backward_scan import JsonlBackwardScanResult
from imbue.mngr.agents.jsonl_backward_scan import JsonlRecordVisitor
from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_file_backward
from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_tail_backward
from imbue.mngr.errors import JsonlFileShrankDuringScanError
from imbue.mngr.interfaces.host import HostFileReadInterface
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

MAX_COMPACTION_TRANSCRIPT_CACHE_ENTRIES: Final[int] = 2048


class CompactionTranscriptScanner(JsonlRecordVisitor, ABC):
    """Finds a transcript's newest assistant-turn timestamp and context token count, visiting records newest-first.

    Each harness subclasses this with its own record matching, filling each value from the
    newest record that yields one and leaving it alone after that. A compaction record newer than
    every record reporting a token count settles the token count as unknown instead.
    """

    latest_assistant_timestamp: datetime | None = Field(
        default=None, description="Timestamp of the newest assistant turn visited so far, if any"
    )
    latest_context_tokens: int | None = Field(
        default=None, description="Prompt context token count of the newest record visited so far that reports one"
    )
    is_context_size_unknown_since_compaction: bool = Field(
        default=False,
        description="Whether a compaction record is newer than every record visited so far that reports a token count",
    )

    def is_satisfied(self) -> bool:
        return self.latest_assistant_timestamp is not None and self.is_context_size_settled()

    def is_context_size_settled(self) -> bool:
        """Whether the records visited so far settle the context size, as a token count or as unknown."""
        return self.latest_context_tokens is not None or self.is_context_size_unknown_since_compaction

    def carries_state_into_older_records(self) -> bool:
        """Whether records visited so far change how a still-missing value would be read from older records.

        A cached value found by an earlier scan was read without the newly appended records, so a
        scanner that returns True here makes an appended-lines update rescan the whole file.
        """
        return False


class CompactionTranscriptReading(FrozenModel):
    """The newest assistant-turn timestamp and context token count found in one transcript."""

    latest_assistant_timestamp: datetime | None = Field(description="Timestamp of the newest assistant turn, if any")
    latest_context_tokens: int | None = Field(
        description="Context token count of the newest turn reporting one, if any"
    )
    is_context_size_unknown_since_compaction: bool = Field(
        default=False,
        description="Whether a compaction is newer than every turn reporting a token count, leaving the count unknown",
    )

    def is_context_size_settled(self) -> bool:
        """Whether the transcript settles the context size, as a token count or as unknown since a compaction."""
        return self.latest_context_tokens is not None or self.is_context_size_unknown_since_compaction


class _CachedTranscriptScan(FrozenModel):
    """What one transcript's latest scan found, and how far into the file it reached."""

    agent_id: AgentId = Field(description="The agent whose transcript this is, for eviction")
    complete_lines_end_byte: int = Field(description="Offset just past the newest complete line scanned")
    file_size: int = Field(description="Size of the file when it was last read")
    reading: CompactionTranscriptReading = Field(description="The values found in the complete lines")


class CompactionTranscriptCache(MutableModel):
    """Caches each transcript's latest scan so a later check reads only the bytes appended since."""

    max_entries: int = Field(frozen=True, description="Entries beyond this many evict the least recently used")

    _entries: dict[tuple[HostId, Path], _CachedTranscriptScan] = PrivateAttr(default_factory=dict)
    _lock: threading.Lock = PrivateAttr(default_factory=threading.Lock)

    def read_transcript(
        self,
        host: HostFileReadInterface,
        host_id: HostId,
        agent_id: AgentId,
        transcript_path: Path,
        scanner_factory: Callable[[], CompactionTranscriptScanner],
    ) -> CompactionTranscriptReading | None:
        """Return the transcript's newest assistant-turn timestamp and context tokens, or None if it does not exist."""
        key = (host_id, transcript_path)
        with self._lock:
            cached = self._entries.get(key)
        try:
            entry = (
                self._scan_whole_transcript(host, agent_id, transcript_path, scanner_factory)
                if cached is None
                else self._update_cached_scan(host, agent_id, transcript_path, scanner_factory, cached)
            )
        except FileNotFoundError:
            self._drop_entry(key)
            return None
        except JsonlFileShrankDuringScanError:
            self._drop_entry(key)
            raise
        self._store_entry(key, entry)
        return entry.reading

    def evict_agents_other_than(self, agent_ids: AbstractSet[AgentId]) -> None:
        """Drop the cached scans of every agent not in agent_ids."""
        with self._lock:
            stale_keys = [key for key, entry in self._entries.items() if entry.agent_id not in agent_ids]
            for key in stale_keys:
                del self._entries[key]

    def _scan_whole_transcript(
        self,
        host: HostFileReadInterface,
        agent_id: AgentId,
        transcript_path: Path,
        scanner_factory: Callable[[], CompactionTranscriptScanner],
    ) -> _CachedTranscriptScan:
        scanner = scanner_factory()
        scan = scan_jsonl_file_backward(host, transcript_path, scanner)
        return _build_cached_scan(agent_id, scan, scanner, older_reading=None)

    def _update_cached_scan(
        self,
        host: HostFileReadInterface,
        agent_id: AgentId,
        transcript_path: Path,
        scanner_factory: Callable[[], CompactionTranscriptScanner],
        cached: _CachedTranscriptScan,
    ) -> _CachedTranscriptScan:
        tail = host.read_file_tail_from_offset(transcript_path, cached.complete_lines_end_byte)
        if tail.file_size < cached.complete_lines_end_byte:
            logger.debug("Transcript {} was truncated or replaced; rescanning it", transcript_path)
            return self._scan_whole_transcript(host, agent_id, transcript_path, scanner_factory)
        if tail.file_size == cached.file_size:
            return cached

        # Visit only the appended lines; values they do not supply come from the cached scan
        scanner = scanner_factory()
        scan = scan_jsonl_tail_backward(
            host=host,
            path=transcript_path,
            visitor=scanner,
            region_start_byte=cached.complete_lines_end_byte,
            tail=tail,
            tail_start_byte=cached.complete_lines_end_byte,
        )
        if scanner.carries_state_into_older_records():
            return self._scan_whole_transcript(host, agent_id, transcript_path, scanner_factory)
        return _build_cached_scan(agent_id, scan, scanner, older_reading=cached.reading)

    def _store_entry(self, key: tuple[HostId, Path], entry: _CachedTranscriptScan) -> None:
        with self._lock:
            # Re-inserting moves the key to the end, so the first key is always the least recently used
            self._entries.pop(key, None)
            self._entries[key] = entry
            while len(self._entries) > self.max_entries:
                del self._entries[next(iter(self._entries))]

    def _drop_entry(self, key: tuple[HostId, Path]) -> None:
        with self._lock:
            self._entries.pop(key, None)


def _build_cached_scan(
    agent_id: AgentId,
    scan: JsonlBackwardScanResult,
    scanner: CompactionTranscriptScanner,
    older_reading: CompactionTranscriptReading | None,
) -> _CachedTranscriptScan:
    """Combine a scan's values with those from the older lines it did not visit, preferring the newer."""
    older_timestamp = None if older_reading is None else older_reading.latest_assistant_timestamp
    timestamp = older_timestamp if scanner.latest_assistant_timestamp is None else scanner.latest_assistant_timestamp
    if older_reading is None or scanner.is_context_size_settled():
        tokens = scanner.latest_context_tokens
        is_context_size_unknown_since_compaction = scanner.is_context_size_unknown_since_compaction
    else:
        tokens = older_reading.latest_context_tokens
        is_context_size_unknown_since_compaction = older_reading.is_context_size_unknown_since_compaction
    return _CachedTranscriptScan(
        agent_id=agent_id,
        complete_lines_end_byte=scan.complete_lines_end_byte,
        file_size=scan.file_size,
        reading=CompactionTranscriptReading(
            latest_assistant_timestamp=timestamp,
            latest_context_tokens=tokens,
            is_context_size_unknown_since_compaction=is_context_size_unknown_since_compaction,
        ),
    )


COMPACTION_TRANSCRIPT_CACHE: Final[CompactionTranscriptCache] = CompactionTranscriptCache(
    max_entries=MAX_COMPACTION_TRANSCRIPT_CACHE_ENTRIES
)
