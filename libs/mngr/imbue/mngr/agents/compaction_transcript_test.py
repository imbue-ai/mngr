import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from pydantic import Field

from imbue.mngr.agents.compaction_transcript import CompactionTranscriptCache
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptReading
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptScanner
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import HostId

_PATH = Path("/host/agents/agent-1/transcript.jsonl")


class _SimpleScanner(CompactionTranscriptScanner):
    """Matches assistant records by role, taking the timestamp and token count from their own fields.

    A "compaction" record newer than every token count leaves the token count unknown.
    """

    def visit_older_record(self, record: dict[str, Any]) -> None:
        if record.get("role") == "compaction" and not self.is_context_size_settled():
            self.is_context_size_unknown_since_compaction = True
        if record.get("role") != "assistant":
            return
        if self.latest_assistant_timestamp is None:
            self.latest_assistant_timestamp = datetime.fromisoformat(record["ts"])
        if not self.is_context_size_settled() and record.get("tokens"):
            self.latest_context_tokens = record["tokens"]


class _StatefulScanner(_SimpleScanner):
    """Reports that a visited "reset" record would change how older records are read."""

    has_seen_reset: bool = Field(default=False)

    def visit_older_record(self, record: dict[str, Any]) -> None:
        if record.get("role") == "reset":
            self.has_seen_reset = True
        super().visit_older_record(record)

    def carries_state_into_older_records(self) -> bool:
        return self.latest_assistant_timestamp is None and self.has_seen_reset


def _assistant_line(minute: int, tokens: int | None) -> bytes:
    record = {"role": "assistant", "ts": f"2026-08-27T12:{minute:02d}:00+00:00", "tokens": tokens}
    return json.dumps(record).encode() + b"\n"


def _user_line(text: str) -> bytes:
    return json.dumps({"role": "user", "text": text}).encode() + b"\n"


def _compaction_line() -> bytes:
    return json.dumps({"role": "compaction"}).encode() + b"\n"


def _at_minute(minute: int) -> datetime:
    return datetime(2026, 8, 27, 12, minute, 0, tzinfo=timezone.utc)


def _new_cache() -> CompactionTranscriptCache:
    return CompactionTranscriptCache(max_entries=64)


def _read(
    cache: CompactionTranscriptCache,
    host: InMemoryHostFileReader,
    host_id: HostId,
    agent_id: AgentId,
    path: Path = _PATH,
) -> CompactionTranscriptReading | None:
    return cache.read_transcript(
        host=host, host_id=host_id, agent_id=agent_id, transcript_path=path, scanner_factory=_SimpleScanner
    )


def test_unchanged_transcript_is_answered_from_the_cache_without_reading_content() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000) + _user_line("hi")})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()

    first = _read(cache, host, host_id, agent_id)
    bytes_after_first_read = host.content_bytes_read
    reads_after_first_read = host.tail_read_count
    second = _read(cache, host, host_id, agent_id)

    assert first == CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(1), latest_context_tokens=1000)
    assert second == first
    assert host.content_bytes_read == bytes_after_first_read
    assert host.tail_read_count == reads_after_first_read + 1


def test_appended_assistant_record_is_read_from_only_the_appended_bytes() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)
    bytes_before_append = host.content_bytes_read

    appended = _user_line("next") + _assistant_line(5, 2500)
    host.append(_PATH, appended)
    reading = _read(cache, host, host_id, agent_id)

    assert reading == CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(5), latest_context_tokens=2500)
    assert host.content_bytes_read - bytes_before_append == len(appended)


def test_appended_lines_without_a_newer_value_keep_the_cached_value() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)

    host.append(_PATH, _assistant_line(7, None) + _user_line("tool result"))
    reading = _read(cache, host, host_id, agent_id)

    assert reading == CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(7), latest_context_tokens=1000)


def test_compaction_newer_than_every_token_count_leaves_the_count_unknown() -> None:
    host = InMemoryHostFileReader(
        contents_by_path={_PATH: _assistant_line(1, 1000) + _compaction_line() + _user_line("summary")}
    )

    reading = _read(_new_cache(), host, HostId.generate(), AgentId.generate())

    assert reading == CompactionTranscriptReading(
        latest_assistant_timestamp=_at_minute(1),
        latest_context_tokens=None,
        is_context_size_unknown_since_compaction=True,
    )


def test_appended_compaction_replaces_the_cached_count_until_a_newer_count_is_appended() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)

    appended_compaction = _compaction_line() + _user_line("summary")
    host.append(_PATH, appended_compaction)
    bytes_before_compaction_read = host.content_bytes_read
    after_compaction = _read(cache, host, host_id, agent_id)
    bytes_for_compaction_read = host.content_bytes_read - bytes_before_compaction_read
    host.append(_PATH, _user_line("more"))
    after_unrelated_lines = _read(cache, host, host_id, agent_id)
    host.append(_PATH, _assistant_line(6, 300))
    after_reply = _read(cache, host, host_id, agent_id)

    unknown_since_compaction = CompactionTranscriptReading(
        latest_assistant_timestamp=_at_minute(1),
        latest_context_tokens=None,
        is_context_size_unknown_since_compaction=True,
    )
    assert after_compaction == unknown_since_compaction
    assert bytes_for_compaction_read == len(appended_compaction)
    assert after_unrelated_lines == unknown_since_compaction
    assert after_reply == CompactionTranscriptReading(
        latest_assistant_timestamp=_at_minute(6), latest_context_tokens=300
    )


def test_partially_written_final_line_is_read_once_it_is_complete() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)

    line = _assistant_line(9, 4000)
    host.append(_PATH, line[:20])
    while_partial = _read(cache, host, host_id, agent_id)
    host.append(_PATH, line[20:])
    once_complete = _read(cache, host, host_id, agent_id)

    assert while_partial == CompactionTranscriptReading(
        latest_assistant_timestamp=_at_minute(1), latest_context_tokens=1000
    )
    assert once_complete == CompactionTranscriptReading(
        latest_assistant_timestamp=_at_minute(9), latest_context_tokens=4000
    )


def test_truncated_transcript_is_rescanned_from_scratch() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _user_line("x" * 500) + _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)

    host.contents_by_path[_PATH] = _assistant_line(3, 300)
    reading = _read(cache, host, host_id, agent_id)

    assert reading == CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(3), latest_context_tokens=300)


def test_stateful_scanner_rescans_the_whole_transcript_when_appended_records_affect_older_ones() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    cache.read_transcript(
        host=host, host_id=host_id, agent_id=agent_id, transcript_path=_PATH, scanner_factory=_StatefulScanner
    )
    tail_reads_before = host.tail_read_count

    host.append(_PATH, json.dumps({"role": "reset"}).encode() + b"\n")
    cache.read_transcript(
        host=host, host_id=host_id, agent_id=agent_id, transcript_path=_PATH, scanner_factory=_StatefulScanner
    )

    # The appended-lines read, then a full rescan's size probe and first window
    assert host.tail_read_count - tail_reads_before == 3


def test_missing_transcript_reads_as_none_and_is_scanned_afresh_once_it_appears() -> None:
    host = InMemoryHostFileReader(contents_by_path={_PATH: _assistant_line(1, 1000)})
    cache = _new_cache()
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id)

    del host.contents_by_path[_PATH]
    while_missing = _read(cache, host, host_id, agent_id)
    host.contents_by_path[_PATH] = _assistant_line(2, 50)
    once_back = _read(cache, host, host_id, agent_id)

    assert while_missing is None
    assert once_back == CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(2), latest_context_tokens=50)


def test_evicting_other_agents_drops_their_entries_but_keeps_the_retained_ones() -> None:
    kept_path = Path("/host/agents/kept/transcript.jsonl")
    removed_path = Path("/host/agents/removed/transcript.jsonl")
    host = InMemoryHostFileReader(
        contents_by_path={kept_path: _assistant_line(1, 1000), removed_path: _assistant_line(2, 2000)}
    )
    cache = _new_cache()
    host_id, kept_agent_id, removed_agent_id = HostId.generate(), AgentId.generate(), AgentId.generate()
    _read(cache, host, host_id, kept_agent_id, kept_path)
    _read(cache, host, host_id, removed_agent_id, removed_path)

    cache.evict_agents_other_than(frozenset({kept_agent_id}))
    bytes_before = host.content_bytes_read
    _read(cache, host, host_id, kept_agent_id, kept_path)
    bytes_for_kept = host.content_bytes_read - bytes_before
    _read(cache, host, host_id, removed_agent_id, removed_path)
    bytes_for_removed = host.content_bytes_read - bytes_before - bytes_for_kept

    assert bytes_for_kept == 0
    assert bytes_for_removed == len(_assistant_line(2, 2000))


def test_cache_beyond_its_cap_evicts_the_least_recently_used_entry() -> None:
    paths = [Path(f"/host/agents/agent-{index}/transcript.jsonl") for index in range(3)]
    host = InMemoryHostFileReader(contents_by_path={path: _assistant_line(1, 1000) for path in paths})
    cache = CompactionTranscriptCache(max_entries=2)
    host_id, agent_id = HostId.generate(), AgentId.generate()
    _read(cache, host, host_id, agent_id, paths[0])
    _read(cache, host, host_id, agent_id, paths[1])
    # Touching the first entry makes the second the least recently used
    _read(cache, host, host_id, agent_id, paths[0])
    _read(cache, host, host_id, agent_id, paths[2])

    bytes_before = host.content_bytes_read
    _read(cache, host, host_id, agent_id, paths[0])
    bytes_for_first = host.content_bytes_read - bytes_before
    _read(cache, host, host_id, agent_id, paths[1])
    bytes_for_second = host.content_bytes_read - bytes_before - bytes_for_first

    assert bytes_for_first == 0
    assert bytes_for_second == len(_assistant_line(1, 1000))


_CONCURRENT_TASKS_PER_TRANSCRIPT = 3
_READS_PER_CONCURRENT_TASK = 5


def _read_repeatedly(
    cache: CompactionTranscriptCache, host: InMemoryHostFileReader, host_id: HostId, agent_id: AgentId, path: Path
) -> list[CompactionTranscriptReading | None]:
    return [_read(cache, host, host_id, agent_id, path) for _ in range(_READS_PER_CONCURRENT_TASK)]


def _read_all_concurrently(
    cache: CompactionTranscriptCache,
    host: InMemoryHostFileReader,
    host_id: HostId,
    agent_id_by_path: dict[Path, AgentId],
) -> dict[Path, list[CompactionTranscriptReading | None]]:
    """Read every transcript from several threads at once, returning every reading each transcript got."""
    with ThreadPoolExecutor(max_workers=16) as executor:
        futures_by_path = {
            path: [
                executor.submit(_read_repeatedly, cache, host, host_id, agent_id, path)
                for _ in range(_CONCURRENT_TASKS_PER_TRANSCRIPT)
            ]
            for path, agent_id in agent_id_by_path.items()
        }
    return {
        path: [reading for future in futures for reading in future.result()]
        for path, futures in futures_by_path.items()
    }


def test_concurrent_reads_of_many_transcripts_keep_each_entry_consistent() -> None:
    paths = [Path(f"/host/agents/agent-{index}/transcript.jsonl") for index in range(40)]
    host = InMemoryHostFileReader(
        contents_by_path={path: _assistant_line(index, 100 + index) for index, path in enumerate(paths)}
    )
    cache = _new_cache()
    host_id = HostId.generate()
    agent_id_by_path = {path: AgentId.generate() for path in paths}

    initial_readings = _read_all_concurrently(cache, host, host_id, agent_id_by_path)
    for path in paths:
        host.append(path, _assistant_line(50, 9000))
    appended_readings = _read_all_concurrently(cache, host, host_id, agent_id_by_path)
    bytes_before_final_reads = host.content_bytes_read
    for path, agent_id in agent_id_by_path.items():
        _read(cache, host, host_id, agent_id, path)

    reads_per_transcript = _CONCURRENT_TASKS_PER_TRANSCRIPT * _READS_PER_CONCURRENT_TASK
    for index, path in enumerate(paths):
        initial = CompactionTranscriptReading(
            latest_assistant_timestamp=_at_minute(index), latest_context_tokens=100 + index
        )
        assert initial_readings[path] == [initial] * reads_per_transcript
        appended = CompactionTranscriptReading(latest_assistant_timestamp=_at_minute(50), latest_context_tokens=9000)
        assert appended_readings[path] == [appended] * reads_per_transcript
    assert host.content_bytes_read == bytes_before_final_reads
