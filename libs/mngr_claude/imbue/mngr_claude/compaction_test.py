import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import Field

from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_file_backward
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNoLongerIdleError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileTailRead
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr_claude.claude_config import IDLE_SINCE_FILENAME
from imbue.mngr_claude.compaction import CLAUDE_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_claude.compaction import ClaudeCompactionTranscriptScanner
from imbue.mngr_claude.compaction import get_agent_context_tokens
from imbue.mngr_claude.compaction import get_agent_idle_since
from imbue.mngr_claude.compaction import get_agent_last_compacted_idle_since
from imbue.mngr_claude.compaction import parse_iso_timestamp
from imbue.mngr_claude.plugin import ClaudeAgent
from imbue.mngr_claude.plugin import ClaudeAgentConfig


class _RecordingHost(FakeHost):
    """Host test double that records text files and checks."""

    files: dict[Path, str] = Field(default_factory=dict)
    mtimes: dict[Path, datetime] = Field(default_factory=dict)
    tail_content_bytes_read: int = 0

    def path_exists(self, path: Path) -> bool:
        return Path(path) in self.files or Path(path) in self.mtimes

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return self.files[Path(path)]

    def read_file_tail_from_offset(self, path: Path, start_byte: int) -> FileTailRead:
        if Path(path) not in self.files:
            raise FileNotFoundError(f"File not found: {path}")
        content = self.files[Path(path)].encode("utf-8")
        tail = content[start_byte:]
        self.tail_content_bytes_read += len(tail)
        return FileTailRead(file_size=len(content), content=tail)

    def write_text_file(
        self,
        path: Path,
        content: str,
        encoding: str = "utf-8",
        mode: str | None = None,
        is_atomic: bool = True,
    ) -> None:
        self.files[Path(path)] = content

    def get_file_mtime(self, path: Path) -> datetime | None:
        return self.mtimes.get(Path(path))

    def run_command(self, *args: object, **kwargs: object) -> CommandResult:
        return CommandResult(
            success=True,
            exit_code=0,
            stdout="MNGR_CONFIRMED pane_activity_probe\n",
            stderr="",
        )

    def execute_stateful_command(self, *args: object, **kwargs: object) -> CommandResult:
        return CommandResult(
            success=True,
            exit_code=0,
            stdout="MNGR_CONFIRMED pane_activity_probe\n",
            stderr="",
        )


class _RecordingClaudeAgent(ClaudeAgent):
    """ClaudeAgent test double that records sent messages and events."""

    sent_messages: list[str] = Field(default_factory=list)
    recorded_events: list[tuple[str, str]] = Field(default_factory=list)
    override_agent_dir: Path = Field(default_factory=Path)

    def is_running(self) -> bool:
        return True

    def _get_agent_dir(self) -> Path:
        return self.override_agent_dir

    def _preflight_send_message(self, tmux_target: TmuxWindowTarget) -> None:
        pass

    def _clear_or_warn_about_preexisting_input_text(self, tmux_target: TmuxWindowTarget) -> None:
        pass

    def _send_tmux_literal_keys(self, tmux_target: TmuxWindowTarget, message: str) -> str:
        self.sent_messages.append(message)
        return ""

    def _capture_pane_content(
        self, tmux_target: TmuxWindowTarget | str, include_scrollback: bool = False
    ) -> str | None:
        return "❯ " + " ".join(self.sent_messages)

    def get_tui_pane_text(self, tmux_target: TmuxWindowTarget | None = None, include_scrollback: bool = False) -> str:
        return "❯ " + " ".join(self.sent_messages)

    def _press_enter(self, tmux_target: TmuxWindowTarget | str) -> None:
        pass

    def record_message_delivery_event(self, event_type: str, detail: str) -> None:
        self.recorded_events.append((event_type, detail))


def _scan_text(raw_text: str) -> ClaudeCompactionTranscriptScanner:
    path = Path("/agent/transcript.jsonl")
    scanner = ClaudeCompactionTranscriptScanner()
    scan_jsonl_file_backward(InMemoryHostFileReader(contents_by_path={path: raw_text.encode("utf-8")}), path, scanner)
    return scanner


def _latest_assistant_timestamp(raw_text: str) -> datetime | None:
    return _scan_text(raw_text).latest_assistant_timestamp


def _latest_context_tokens(raw_text: str) -> int | None:
    return _scan_text(raw_text).latest_context_tokens


def test_parse_iso_timestamp() -> None:
    # RFC3339 with Z
    dt = parse_iso_timestamp("2026-08-27T12:00:00Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # With nanoseconds truncated to microseconds
    dt = parse_iso_timestamp("2026-08-27T12:00:00.123456789Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, 123456, tzinfo=timezone.utc)

    # Invalid timestamp
    assert parse_iso_timestamp("invalid-date") is None
    assert parse_iso_timestamp("") is None


def test_get_agent_idle_since(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    # When no idle_since file exists
    assert get_agent_idle_since(agent) is None

    # When active marker is present
    host.files[tmp_path / "active"] = ""
    host.files[tmp_path / IDLE_SINCE_FILENAME] = "2026-08-27T12:00:00Z"
    assert get_agent_idle_since(agent) is None

    # When active marker is gone and idle_since file exists
    del host.files[tmp_path / "active"]
    idle_dt = get_agent_idle_since(agent)
    assert idle_dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # When idle_since is deleted but transcript assistant turn exists
    del host.files[tmp_path / IDLE_SINCE_FILENAME]
    transcript_path = tmp_path / "logs/claude_transcript/events.jsonl"
    host.files[transcript_path] = '{"type": "assistant", "timestamp": "2026-08-27T12:30:00Z"}\n'
    fallback_dt = get_agent_idle_since(agent)
    assert fallback_dt == datetime(2026, 8, 27, 12, 30, 0, tzinfo=timezone.utc)

    # When transcript is deleted but session marker exists
    del host.files[transcript_path]
    session_started_path = tmp_path / "session_started"
    host.files[session_started_path] = ""
    host.mtimes[session_started_path] = datetime(2026, 8, 27, 12, 45, 0, tzinfo=timezone.utc)
    fallback_mtime_dt = get_agent_idle_since(agent)
    assert fallback_mtime_dt == datetime(2026, 8, 27, 12, 45, 0, tzinfo=timezone.utc)


def test_scanner_finds_latest_assistant_timestamp() -> None:
    assert _latest_assistant_timestamp("") is None
    assert _latest_assistant_timestamp("not json\n") is None

    # Multi-turn transcript
    raw_transcript = (
        '{"type": "user", "timestamp": "2026-08-27T12:00:00Z", "message": {"content": "hello"}}\n'
        '{"type": "assistant", "timestamp": "2026-08-27T12:01:00Z", "message": {"usage": {"input_tokens": 100}}}\n'
        '{"type": "user", "timestamp": "2026-08-27T12:05:00Z", "text": "/compact"}\n'
    )
    # Returns the timestamp of the assistant turn (not the /compact command)
    assert _latest_assistant_timestamp(raw_transcript) == datetime(2026, 8, 27, 12, 1, 0, tzinfo=timezone.utc)


def test_scanner_finds_latest_context_tokens() -> None:
    assert _latest_context_tokens("") is None
    assert _latest_context_tokens("not json\n") is None

    # Raw transcript format with caching
    raw_transcript = (
        '{"type": "user", "message": {"content": "hello"}}\n'
        '{"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 120000, "output_tokens": 100}}}\n'
    )
    assert _latest_context_tokens(raw_transcript) == 121502

    # Multiple assistant messages -> returns latest
    multi_turn = (
        '{"type": "assistant", "message": {"usage": {"input_tokens": 50000}}}\n'
        '{"type": "user", "message": {"content": "more"}}\n'
        '{"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 80000, "cache_creation_input_tokens": 2000}}}\n'
    )
    assert _latest_context_tokens(multi_turn) == 82010

    # Common transcript format
    common_transcript = (
        '{"type": "user_message", "text": "do something"}\n'
        '{"type": "assistant_message", "usage": {"input_tokens": 5, "cache_read_tokens": 90000, "cache_write_tokens": 1000, "output_tokens": 50}}\n'
    )
    assert _latest_context_tokens(common_transcript) == 91005


def test_get_agent_context_tokens(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    # When no transcript exists
    assert get_agent_context_tokens(agent) is None

    # When raw transcript exists
    transcript_path = tmp_path / "logs/claude_transcript/events.jsonl"
    host.files[transcript_path] = (
        '{"type": "assistant", "message": {"usage": {"input_tokens": 100, "cache_read_input_tokens": 105000}}}\n'
    )
    assert get_agent_context_tokens(agent) == 105100


def test_claude_agent_compaction_capability(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    compaction_agent = require_compaction_agent(agent)
    assert compaction_agent.get_cache_ttl_minutes() == CLAUDE_DEFAULT_CACHE_TTL_MINUTES

    # Initially not idle
    assert compaction_agent.get_idle_since() is None

    # Set idle
    idle_dt = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = idle_dt.isoformat()
    assert compaction_agent.get_idle_since() == idle_dt

    # Set transcript tokens
    transcript_path = tmp_path / "logs/claude_transcript/events.jsonl"
    host.files[transcript_path] = (
        '{"type": "assistant", "message": {"usage": {"input_tokens": 500, "cache_read_input_tokens": 99500}}}\n'
    )
    assert compaction_agent.get_context_tokens() == 100000

    # Request compaction
    compaction_agent.request_compaction()
    assert "/compact" in agent.sent_messages
    last_compacted = get_agent_last_compacted_idle_since(agent)
    assert last_compacted is not None and last_compacted >= idle_dt

    # After compaction, get_idle_since returns None for the same epoch
    assert compaction_agent.get_idle_since() is None

    # When new activity occurs (newer timestamp), get_idle_since returns the new epoch
    new_idle_dt = last_compacted + timedelta(hours=1)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = new_idle_dt.isoformat()
    assert compaction_agent.get_idle_since() == new_idle_dt

    # Request compaction with custom instructions
    compaction_agent.request_compaction(instructions="preserve git status and bug details")
    assert "/compact preserve git status and bug details" in agent.sent_messages

    # Request compaction with blank/whitespace instructions falls back to /compact
    compaction_agent.request_compaction(instructions="   ")
    assert agent.sent_messages[-1] == "/compact"


def _reference_latest_assistant_timestamp(raw_text: str) -> datetime | None:
    """A whole-file, newest-first parser of the newest assistant timestamp, kept as an oracle."""
    for line in reversed(raw_text.strip().splitlines()):
        stripped = line.strip()
        record = json.loads(stripped) if stripped else None
        if isinstance(record, dict) and record.get("type") in ("assistant", "assistant_message"):
            ts_str = record.get("timestamp")
            dt = parse_iso_timestamp(ts_str) if isinstance(ts_str, str) else None
            if dt is not None:
                return dt
    return None


def _reference_context_tokens(raw_text: str) -> int | None:
    """A whole-file, newest-first parser of the context size, kept as an oracle.

    A compaction newer than any usage leaves the size unknown.
    """
    for line in reversed(raw_text.strip().splitlines()):
        stripped = line.strip()
        record = json.loads(stripped) if stripped else None
        if not isinstance(record, dict):
            continue
        if record.get("type") == "system" and record.get("subtype") == "compact_boundary":
            return None
        usage: Any = None
        if record.get("type") in ("assistant", "assistant_message"):
            msg = record.get("message")
            if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
                usage = msg["usage"]
            elif isinstance(record.get("usage"), dict):
                usage = record["usage"]
            else:
                usage = None
        elif isinstance(record.get("usage"), dict):
            usage = record["usage"]
        else:
            usage = None
        if usage is not None:
            total = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or usage.get("cache_read_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or usage.get("cache_write_tokens") or 0)
            )
            if total > 0:
                return total
    return None


def _jsonl(*records: dict[str, Any]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _user_prompt(minute: int, text: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": f"2026-08-27T12:{minute:02d}:00.000Z",
        "message": {"role": "user", "content": text},
    }


def _assistant_turn(minute: int, input_tokens: int, cache_read: int, cache_creation: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": f"2026-08-27T12:{minute:02d}:30.000Z",
        "message": {
            "model": "claude-opus-4-8",
            "role": "assistant",
            "content": [{"type": "tool_use", "id": f"toolu_{minute}", "name": "Bash", "input": {"command": "ls"}}],
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "output_tokens": 412,
            },
        },
    }


def _tool_result(minute: int, output: str) -> dict[str, Any]:
    return {
        "type": "user",
        "timestamp": f"2026-08-27T12:{minute:02d}:45.000Z",
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": f"toolu_{minute}", "content": output}],
        },
        "toolUseResult": {"stdout": output, "stderr": "", "interrupted": False},
    }


def _compact_boundary(minute: int, pre_tokens: int = 182000, post_tokens: int = 12000) -> dict[str, Any]:
    return {
        "type": "system",
        "subtype": "compact_boundary",
        "timestamp": f"2026-08-27T12:{minute:02d}:10.000Z",
        "content": "Conversation compacted",
        "compactMetadata": {"trigger": "manual", "preTokens": pre_tokens, "postTokens": post_tokens},
    }


def _compact_summary_records(minute: int) -> list[dict[str, Any]]:
    """The records Claude writes after a compact boundary, stamped earlier than the boundary itself."""
    return [
        {
            "type": "user",
            "timestamp": f"2026-08-27T12:{minute:02d}:05.000Z",
            "isCompactSummary": True,
            "message": {"role": "user", "content": "This session is being continued from a previous conversation."},
        },
        {
            "type": "user",
            "timestamp": f"2026-08-27T12:{minute:02d}:01.000Z",
            "isMeta": True,
            "message": {"role": "user", "content": "<command-name>/compact</command-name>"},
        },
        {"type": "attachment", "timestamp": f"2026-08-27T12:{minute:02d}:06.000Z", "attachment": {"type": "todo"}},
    ]


def _api_error_turn(minute: int) -> dict[str, Any]:
    return {
        "type": "assistant",
        "timestamp": f"2026-08-27T12:{minute:02d}:50.000Z",
        "isApiErrorMessage": True,
        "message": {
            "model": "<synthetic>",
            "role": "assistant",
            "content": [{"type": "text", "text": "API Error: 529 overloaded"}],
            "usage": {
                "input_tokens": 0,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": 0,
            },
        },
    }


_CLAUDE_TRANSCRIPT_FIXTURES = {
    "tool_results_after_last_assistant_turn": _jsonl(
        _user_prompt(1, "run the tests"),
        _assistant_turn(1, 3, 120000, 1500),
        _tool_result(1, "ok"),
        _assistant_turn(2, 5, 121000, 900),
        _tool_result(2, "x" * 200_000),
        _tool_result(3, "y" * 50_000),
    ),
    "compact_boundary_then_new_turn": _jsonl(
        _user_prompt(1, "hello"),
        _assistant_turn(1, 2, 180000, 2000),
        _compact_boundary(5),
        _user_prompt(5, "This session is being continued from a previous conversation."),
        _assistant_turn(6, 10, 0, 21000),
    ),
    "compact_boundary_after_last_turn": _jsonl(
        _user_prompt(1, "hello"),
        _assistant_turn(1, 2, 180000, 2000),
        _compact_boundary(5),
    ),
    "api_error_with_zero_usage_is_newest": _jsonl(
        _user_prompt(1, "hello"),
        _assistant_turn(1, 4, 99000, 1000),
        _user_prompt(2, "again"),
        _api_error_turn(2),
    ),
    "common_transcript_format": _jsonl(
        {"type": "user_message", "timestamp": "2026-08-27T12:01:00Z", "text": "do something"},
        {
            "type": "assistant_message",
            "timestamp": "2026-08-27T12:02:00Z",
            "usage": {"input_tokens": 5, "cache_read_tokens": 90000, "cache_write_tokens": 1000},
        },
        {"type": "tool_result", "timestamp": "2026-08-27T12:03:00Z", "output": "z" * 100_000},
    ),
    "no_assistant_turn": _jsonl(_user_prompt(1, "hello"), _tool_result(1, "nothing")),
}


@pytest.mark.parametrize("fixture_name", sorted(_CLAUDE_TRANSCRIPT_FIXTURES))
def test_agent_compaction_values_match_the_whole_file_parsers(
    fixture_name: str, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    transcript = _CLAUDE_TRANSCRIPT_FIXTURES[fixture_name]
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / "logs/claude_transcript/events.jsonl"] = transcript

    assert get_agent_idle_since(agent) == _reference_latest_assistant_timestamp(transcript)
    assert get_agent_context_tokens(agent) == _reference_context_tokens(transcript)


def test_agent_values_come_from_whichever_transcript_has_them(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    # The raw transcript has a turn timestamp but no usage; the common transcript has both
    host.files[tmp_path / "logs/claude_transcript/events.jsonl"] = _jsonl(
        {"type": "assistant", "timestamp": "2026-08-27T12:30:00Z", "message": {"content": "hi"}}
    )
    host.files[tmp_path / "events/claude/common_transcript/events.jsonl"] = _jsonl(
        {"type": "assistant_message", "timestamp": "2026-08-27T12:00:00Z", "usage": {"input_tokens": 70000}}
    )

    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 30, 0, tzinfo=timezone.utc)
    assert get_agent_context_tokens(agent) == 70000


def test_agent_evaluation_of_a_large_transcript_reads_a_bounded_amount(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    turns = [
        _jsonl(
            _user_prompt(minute % 60, "next"),
            _assistant_turn(minute % 60, 7, 150000 + minute, 800),
            _tool_result(minute % 60, "o" * 20_000),
        )
        for minute in range(1000)
    ]
    transcript = "".join(turns) + _jsonl(_assistant_turn(59, 9, 201000, 1200), _tool_result(59, "done"))
    assert len(transcript) > 20 * 1024 * 1024
    host.files[tmp_path / "logs/claude_transcript/events.jsonl"] = transcript

    idle_since = get_agent_idle_since(agent)
    context_tokens = get_agent_context_tokens(agent)
    bytes_for_first_evaluation = host.tail_content_bytes_read
    get_agent_idle_since(agent)
    get_agent_context_tokens(agent)

    assert idle_since == datetime(2026, 8, 27, 12, 59, 30, tzinfo=timezone.utc)
    assert context_tokens == 9 + 201000 + 1200
    assert bytes_for_first_evaluation < 1024 * 1024
    assert host.tail_content_bytes_read == bytes_for_first_evaluation


def _make_agent(tmp_path: Path, mngr_ctx: MngrContext, host: _RecordingHost) -> _RecordingClaudeAgent:
    return _RecordingClaudeAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-agent"),
        agent_type=AgentTypeName("claude"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=mngr_ctx,
        agent_config=ClaudeAgentConfig(check_installation=False, preserve_sessions_on_destroy=False),
        host=host,
        override_agent_dir=tmp_path,
    )


_COMPACTION_IDLE_SINCE = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_idle_recording_claude_agent(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> tuple[_RecordingHost, _RecordingClaudeAgent]:
    host = _RecordingHost(host_dir=tmp_path)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = _COMPACTION_IDLE_SINCE.isoformat()
    return host, _make_agent(tmp_path, temp_mngr_ctx, host)


def test_claude_request_compaction_gives_up_on_a_message_lock_another_process_holds(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    _host, agent = _make_idle_recording_claude_agent(tmp_path, temp_mngr_ctx)

    with file_lock_held_by_another_process(tmp_path / "message.lock", temp_mngr_ctx.concurrency_group):
        with pytest.raises(MessageLockTimeoutError):
            agent.request_compaction(message_lock_timeout_seconds=0.5, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert agent.sent_messages == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_claude_request_compaction_skips_an_agent_that_became_active(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host, agent = _make_idle_recording_claude_agent(tmp_path, temp_mngr_ctx)
    host.files[tmp_path / "active"] = ""

    with pytest.raises(AgentNoLongerIdleError, match="now not idle"):
        agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert agent.sent_messages == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_claude_request_compaction_sends_compact_when_still_idle_since_the_expected_moment(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    _host, agent = _make_idle_recording_claude_agent(tmp_path, temp_mngr_ctx)

    agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert agent.sent_messages == ["/compact"]
    assert get_agent_last_compacted_idle_since(agent) is not None


_CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH = "logs/claude_transcript/events.jsonl"


def _assistant_turn_with_context(minute: int, context_tokens: int) -> dict[str, Any]:
    return _assistant_turn(minute, 6, context_tokens - 2006, 2000)


def _compacted_transcript_records() -> list[dict[str, Any]]:
    """A 210k-token turn, then a manual compaction that no reply has followed yet."""
    return [
        _user_prompt(1, "keep going"),
        _assistant_turn_with_context(1, 210000),
        _compact_boundary(5, pre_tokens=210400, post_tokens=12000),
        *_compact_summary_records(5),
    ]


def test_context_size_is_unknown_after_a_compact_boundary_until_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(*_compacted_transcript_records())

    assert get_agent_context_tokens(agent) is None
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 1, 30, tzinfo=timezone.utc)


def test_context_size_after_a_compact_boundary_comes_from_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(
        *_compacted_transcript_records(), _user_prompt(6, "next"), _assistant_turn_with_context(6, 30000)
    )

    assert get_agent_context_tokens(agent) == 30000


def test_context_size_stays_unknown_across_two_compactions_with_no_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(
        *_compacted_transcript_records(), _compact_boundary(8, pre_tokens=12500, post_tokens=9000)
    )

    assert get_agent_context_tokens(agent) is None


def test_compact_boundary_is_ordered_by_file_position_not_timestamp(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    reply_host = _RecordingHost(host_dir=tmp_path / "reply")
    reply_agent = _make_agent(tmp_path / "reply", temp_mngr_ctx, reply_host)
    boundary_host = _RecordingHost(host_dir=tmp_path / "boundary")
    boundary_agent = _make_agent(tmp_path / "boundary", temp_mngr_ctx, boundary_host)
    # A reply written after the boundary counts even though its timestamp is earlier than the boundary's
    reply_host.files[tmp_path / "reply" / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(
        _assistant_turn_with_context(1, 210000), _compact_boundary(9), _assistant_turn_with_context(8, 30000)
    )
    # A boundary written after a reply hides it even though its timestamp is earlier than the reply's
    boundary_host.files[tmp_path / "boundary" / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(
        _assistant_turn_with_context(9, 210000), _compact_boundary(2)
    )

    assert get_agent_context_tokens(reply_agent) == 30000
    assert get_agent_context_tokens(boundary_agent) is None


def test_appended_compact_boundary_replaces_a_cached_context_size_until_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    transcript_path = tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH
    host.files[transcript_path] = _jsonl(_user_prompt(1, "keep going"), _assistant_turn_with_context(1, 210000))
    before_compaction = agent.get_context_tokens()

    appended_compaction = _jsonl(_compact_boundary(5), *_compact_summary_records(5))
    host.files[transcript_path] += appended_compaction
    bytes_before_compaction_read = host.tail_content_bytes_read
    after_compaction = agent.get_context_tokens()
    bytes_for_compaction_read = host.tail_content_bytes_read - bytes_before_compaction_read

    host.files[transcript_path] += _jsonl(_user_prompt(6, "next"), _assistant_turn_with_context(6, 30000))
    after_reply = agent.get_context_tokens()

    assert before_compaction == 210000
    assert after_compaction is None
    assert bytes_for_compaction_read == len(appended_compaction.encode("utf-8"))
    assert after_reply == 30000


def test_suppressed_context_size_is_logged_at_debug_with_the_agent_and_transcript(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    transcript_path = tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH
    host.files[transcript_path] = _jsonl(*_compacted_transcript_records())

    with capture_loguru(level="DEBUG") as log_output:
        get_agent_context_tokens(agent)

    assert (
        f"Context size of agent test-agent is unknown until its next reply: {transcript_path} has a compaction"
        in log_output.getvalue()
    )


def test_compaction_in_the_preferred_transcript_is_not_overridden_by_an_older_fallback_transcript(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / _CLAUDE_RAW_TRANSCRIPT_RELATIVE_PATH] = _jsonl(*_compacted_transcript_records())
    host.files[tmp_path / "transcript.jsonl"] = _jsonl(_assistant_turn_with_context(1, 210000))

    assert get_agent_context_tokens(agent) is None
