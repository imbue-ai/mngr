import json
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

from imbue.mngr.agents.compaction_transcript import CompactionTranscriptCache
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptReading
from imbue.mngr.agents.compaction_transcript import CompactionTranscriptScanner
from imbue.mngr.hosts.host import Host
from imbue.mngr.primitives import AgentId


class _AssistantRecordScanner(CompactionTranscriptScanner):
    """Takes the timestamp and token count from assistant records' own fields."""

    def visit_older_record(self, record: dict[str, Any]) -> None:
        if record.get("role") != "assistant":
            return
        if self.latest_assistant_timestamp is None:
            self.latest_assistant_timestamp = datetime.fromisoformat(record["ts"])
        if self.latest_context_tokens is None:
            self.latest_context_tokens = record["tokens"]


def _assistant_line(minute: int, tokens: int) -> str:
    return json.dumps({"role": "assistant", "ts": f"2026-08-27T12:{minute:02d}:00+00:00", "tokens": tokens}) + "\n"


def _read(
    cache: CompactionTranscriptCache, host: Host, agent_id: AgentId, transcript_path: Path
) -> CompactionTranscriptReading | None:
    return cache.read_transcript(
        host=host,
        host_id=host.id,
        agent_id=agent_id,
        transcript_path=transcript_path,
        scanner_factory=_AssistantRecordScanner,
    )


def test_local_host_transcript_reads_follow_appends_and_removal(local_host: Host) -> None:
    transcript_path = local_host.host_dir / "agents" / "transcript-reader" / "events.jsonl"
    transcript_path.parent.mkdir(parents=True)
    filler = "".join(json.dumps({"role": "user", "text": "f" * 300}) + "\n" for _ in range(2000))
    transcript_path.write_text(_assistant_line(1, 1000) + filler)
    cache = CompactionTranscriptCache(max_entries=8)
    agent_id = AgentId.generate()

    initial = _read(cache, local_host, agent_id, transcript_path)
    with transcript_path.open("a") as transcript_file:
        transcript_file.write(_assistant_line(8, 8000))
    after_append = _read(cache, local_host, agent_id, transcript_path)
    transcript_path.unlink()
    after_removal = _read(cache, local_host, agent_id, transcript_path)

    assert initial == CompactionTranscriptReading(
        latest_assistant_timestamp=datetime(2026, 8, 27, 12, 1, tzinfo=timezone.utc), latest_context_tokens=1000
    )
    assert after_append == CompactionTranscriptReading(
        latest_assistant_timestamp=datetime(2026, 8, 27, 12, 8, tzinfo=timezone.utc), latest_context_tokens=8000
    )
    assert after_removal is None
