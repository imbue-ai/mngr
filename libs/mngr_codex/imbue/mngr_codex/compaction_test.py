from __future__ import annotations

import json
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_file_backward
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNoLongerIdleError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.errors import SendMessageError
from imbue.mngr.interfaces.agent import AgentLifecycleState
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileTailRead
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr_codex.app_server_client import CodexAppServerError
from imbue.mngr_codex.codex_config import COMMON_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_codex.codex_config import IDLE_SINCE_FILENAME
from imbue.mngr_codex.codex_config import LAST_COMPACTED_IDLE_SINCE_FILENAME
from imbue.mngr_codex.codex_config import RAW_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_codex.codex_config import TRANSCRIPT_PATH_FILENAME
from imbue.mngr_codex.compaction import CODEX_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_codex.compaction import CodexCompactionTranscriptScanner
from imbue.mngr_codex.compaction import get_agent_context_tokens
from imbue.mngr_codex.compaction import get_agent_idle_since
from imbue.mngr_codex.compaction import get_agent_last_compacted_idle_since
from imbue.mngr_codex.compaction import parse_iso_timestamp
from imbue.mngr_codex.compaction import record_agent_compacted
from imbue.mngr_codex.plugin import CodexAgent
from imbue.mngr_codex.plugin import CodexAgentConfig


class _RecordingHost(FakeHost):
    """Host test double that records text files and checks."""

    files: dict[Path, str] = Field(default_factory=dict)
    mtimes: dict[Path, datetime] = Field(default_factory=dict)
    raise_on_read: set[Path] = Field(default_factory=set)
    raise_on_exists: bool = False

    def path_exists(self, path: Path) -> bool:
        if self.raise_on_exists:
            raise OSError("Simulated path_exists failure")
        return Path(path) in self.files or Path(path) in self.mtimes

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        if Path(path) in self.raise_on_read:
            raise OSError("Simulated read failure")
        return self.files[Path(path)]

    def read_file_tail_from_offset(self, path: Path, start_byte: int) -> FileTailRead:
        if Path(path) in self.raise_on_read:
            raise OSError("Simulated read failure")
        if Path(path) not in self.files:
            raise FileNotFoundError(f"File not found: {path}")
        content = self.files[Path(path)].encode("utf-8")
        return FileTailRead(file_size=len(content), content=content[start_byte:])

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
            stdout="",
            stderr="",
        )


class _MockAppServerClient:
    """Mock CodexAppServerClient recording compaction calls."""

    def __init__(self, should_fail: bool = False) -> None:
        self.compact_calls: list[str | None] = []
        self.closed: bool = False
        self.should_fail: bool = should_fail

    def thread_compact_start(self, thread_id: str | None = None) -> dict[str, Any]:
        if self.should_fail:
            raise CodexAppServerError(code=-32600, message="Compaction failed")
        self.compact_calls.append(thread_id)
        return {}

    def close(self) -> None:
        self.closed = True


class _RecordingCodexAgent(CodexAgent):
    """CodexAgent test double that intercepts app-server calls."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    mock_client: _MockAppServerClient | None = None
    override_agent_dir: Path = Field(default_factory=Path)
    mock_lifecycle_state: AgentLifecycleState = AgentLifecycleState.WAITING

    def _get_agent_dir(self) -> Path:
        return self.override_agent_dir

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.mock_lifecycle_state

    def _open_app_server_client(self) -> Any:
        assert self.mock_client is not None
        return self.mock_client

    def _bind_thread_for_send(self, client: Any) -> None:
        pass


def _scan_text(raw_text: str) -> CodexCompactionTranscriptScanner:
    path = Path("/agent/rollout.jsonl")
    scanner = CodexCompactionTranscriptScanner()
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

    # With +00:00
    dt = parse_iso_timestamp("2026-08-27T12:00:00+00:00")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Subseconds
    dt = parse_iso_timestamp("2026-08-27T12:00:00.123456Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, 123456, tzinfo=timezone.utc)

    # Without timezone (naive ISO string is assigned UTC)
    dt = parse_iso_timestamp("2026-08-27T12:00:00")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Invalid timestamp
    assert parse_iso_timestamp("invalid-date") is None
    assert parse_iso_timestamp("") is None
    assert parse_iso_timestamp("   ") is None


def test_scanner_finds_latest_assistant_timestamp() -> None:
    assert _latest_assistant_timestamp("") is None
    assert _latest_assistant_timestamp("not json\n") is None
    assert _latest_assistant_timestamp('\n  \n  123\n  "hello"\n') is None
    assert _latest_assistant_timestamp('{"type": "other"}\n   \n{"type": "other"}') is None

    # Multi-turn rollout events
    raw_transcript = (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:00:00Z", "payload": {"type": "user_message", "message": "hello"}}\n'
        '{"type": "response_item", "timestamp": "2026-08-27T12:01:00Z", "payload": {"role": "assistant", "type": "message", "content": "hi"}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:00Z", "payload": {"type": "task_complete", "completed_at": 1787832120}}\n'
    )
    expected = datetime.fromtimestamp(1787832120, tz=timezone.utc)
    assert _latest_assistant_timestamp(raw_transcript) == expected

    # Compacted and token usage events alone are NOT assistant activity
    compacted_transcript = '{"type": "compacted", "timestamp": "2026-08-27T13:00:00Z"}\n'
    assert _latest_assistant_timestamp(compacted_transcript) is None

    usage_transcript = '{"type": "token_usage_record", "timestamp": "2026-08-27T14:00:00Z", "payload": {"usage": {"input_tokens": 500}}}\n'
    assert _latest_assistant_timestamp(usage_transcript) is None

    # Real turn followed by compaction ignores the compaction turn and keeps the real assistant timestamp
    post_compaction_transcript = (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:00Z", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "hi", "completed_at": 1787832120}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:05:00Z", "payload": {"type": "item_completed", "turn_id": "turn-compact", "item": {"type": "ContextCompaction"}}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:05:01Z", "payload": {"type": "task_complete", "turn_id": "turn-compact", "last_agent_message": null, "completed_at": 1787832301}}\n'
        '{"type": "compacted", "timestamp": "2026-08-27T12:05:02Z", "payload": {"latest_token_usage_record": {"turn_id": "turn-compact"}}}\n'
    )
    assert _latest_assistant_timestamp(post_compaction_transcript) == expected

    # Compaction task_complete without compacted record is also ignored
    compaction_no_compacted = (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:00Z", "payload": {"type": "task_complete", "turn_id": "turn-1", "last_agent_message": "hi", "completed_at": 1787832120}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:05:01Z", "payload": {"type": "task_complete", "turn_id": "turn-compact-2", "last_agent_message": null, "completed_at": 1787832301}}\n'
    )
    assert _latest_assistant_timestamp(compaction_no_compacted) == expected

    # response_item variations (assistant role, reasoning, function_call, custom_tool_call)
    for ptype in ("reasoning", "function_call", "custom_tool_call"):
        tr = f'{{"type": "response_item", "timestamp": "2026-08-27T12:00:00Z", "payload": {{"type": "{ptype}"}}}}\n'
        assert _latest_assistant_timestamp(tr) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # response_item from user is ignored
    tr_user = '{"type": "response_item", "timestamp": "2026-08-27T12:00:00Z", "payload": {"role": "user", "type": "user_message"}}\n'
    assert _latest_assistant_timestamp(tr_user) is None

    # assistant and assistant_message event types
    tr_asst = '{"type": "assistant", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_asst) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    tr_asst_msg = '{"type": "assistant_message", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_asst_msg) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # step event types (agent, assistant vs user)
    tr_step_agent = '{"type": "step", "source": "agent", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_step_agent) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    tr_step_asst = '{"type": "step", "source": "assistant", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_step_asst) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    tr_step_user = '{"type": "step", "source": "user", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_step_user) is None

    # observation event type
    tr_obs = '{"type": "observation", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_obs) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # unknown event type is ignored
    tr_unk = '{"type": "unknown_event", "timestamp": "2026-08-27T12:00:00Z"}\n'
    assert _latest_assistant_timestamp(tr_unk) is None

    # timestamp in payload rather than root record
    tr_payload_ts = (
        '{"type": "event_msg", "payload": {"type": "agent_message", "timestamp": "2026-08-27T12:00:00Z"}}\n'
    )
    assert _latest_assistant_timestamp(tr_payload_ts) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # completed_at overflow falls back to timestamp
    tr_overflow = '{"type": "event_msg", "timestamp": "2026-08-27T12:00:00Z", "payload": {"type": "task_complete", "completed_at": 1e50}}\n'
    assert _latest_assistant_timestamp(tr_overflow) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def test_scanner_finds_latest_context_tokens() -> None:
    assert _latest_context_tokens("") is None
    assert _latest_context_tokens("not json\n") is None
    assert _latest_context_tokens('\n  \n123\n"text"\n') is None
    assert _latest_context_tokens('{"type": "other"}\n   \n{"type": "other"}') is None

    # Codex token_count format with last_token_usage
    transcript = '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1234, "output_tokens": 50}, "total_token_usage": {"input_tokens": 5000, "output_tokens": 200}}}}\n'
    assert _latest_context_tokens(transcript) == 1234

    # Post-compaction token_count with input_tokens=0 but total_tokens > 0 uses total_tokens
    compaction_token_count = '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 0, "total_tokens": 5565}, "total_token_usage": {"input_tokens": 84809, "total_tokens": 84919}}}}\n'
    assert _latest_context_tokens(compaction_token_count) == 5565

    # Codex token_count format with total_token_usage fallback
    transcript_fallback = '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 0}, "total_token_usage": {"input_tokens": 4321}}}}\n'
    assert _latest_context_tokens(transcript_fallback) == 4321

    # Multiple events returns the latest
    multi_turn = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 1000}}}}\n'
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 2500}}}}\n'
    )
    assert _latest_context_tokens(multi_turn) == 2500


def test_get_agent_context_tokens(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    # When no transcript exists
    assert get_agent_context_tokens(agent) is None

    # When raw transcript exists under logs/codex_transcript/events.jsonl
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 15000}}}}\n'
    )
    assert get_agent_context_tokens(agent) == 15000

    # When raw transcript is absent, but rollout pointer file exists
    del host.files[raw_path]
    rollout_file = tmp_path / "sessions/rollout.jsonl"
    host.files[rollout_file] = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 22000}}}}\n'
    )
    host.files[tmp_path / TRANSCRIPT_PATH_FILENAME] = str(rollout_file)
    assert get_agent_context_tokens(agent) == 22000

    # When rollout pointer read fails with OSError, it falls through
    del host.files[tmp_path / TRANSCRIPT_PATH_FILENAME]
    host.files[tmp_path / TRANSCRIPT_PATH_FILENAME] = "invalid"
    host.raise_on_read.add(tmp_path / TRANSCRIPT_PATH_FILENAME)
    # Check fallback to events/codex/usage/events.jsonl
    events_path = tmp_path / "events/codex/usage/events.jsonl"
    host.files[events_path] = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 31000}}}}\n'
    )
    assert get_agent_context_tokens(agent) == 31000
    host.raise_on_read.remove(tmp_path / TRANSCRIPT_PATH_FILENAME)

    # Check fallback to transcript.jsonl
    del host.files[events_path]
    del host.files[tmp_path / TRANSCRIPT_PATH_FILENAME]
    host.files[tmp_path / "transcript.jsonl"] = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 42000}}}}\n'
    )
    assert get_agent_context_tokens(agent) == 42000

    # Exception during context token check returns None
    host.raise_on_exists = True
    assert get_agent_context_tokens(agent) is None
    host.raise_on_exists = False


def test_get_agent_last_compacted_idle_since(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
    )
    # When file doesn't exist
    assert get_agent_last_compacted_idle_since(agent) is None

    # When file exists
    host.files[tmp_path / LAST_COMPACTED_IDLE_SINCE_FILENAME] = "2026-08-27T12:00:00Z"
    assert get_agent_last_compacted_idle_since(agent) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # When reading file raises OSError
    host.raise_on_read.add(tmp_path / LAST_COMPACTED_IDLE_SINCE_FILENAME)
    assert get_agent_last_compacted_idle_since(agent) is None


def test_record_agent_compacted(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    # Record with naive timestamp (should convert to UTC)
    naive_dt = datetime(2026, 8, 27, 12, 0, 0)
    record_agent_compacted(agent, idle_since=naive_dt)
    assert get_agent_last_compacted_idle_since(agent) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Record with default (None) sets a valid current timestamp
    record_agent_compacted(agent)
    recorded = get_agent_last_compacted_idle_since(agent)
    assert recorded is not None
    assert recorded.tzinfo == timezone.utc


def test_get_agent_idle_since(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )

    # When no idle_since or transcript exists
    assert get_agent_idle_since(agent) is None

    # When agent is RUNNING, always returns None even if idle_since file exists
    host.files[tmp_path / IDLE_SINCE_FILENAME] = "2026-08-27T12:00:00Z"
    agent.mock_lifecycle_state = AgentLifecycleState.RUNNING
    assert get_agent_idle_since(agent) is None

    # When agent transitions to WAITING, returns idle_since
    agent.mock_lifecycle_state = AgentLifecycleState.WAITING
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # When raw transcript has an assistant event, that takes precedence over fallback marker
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = (
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}, "timestamp": "2026-08-27T12:30:00Z"}\n'
    )
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 30, 0, tzinfo=timezone.utc)

    # When rollout pointer points to a valid file
    del host.files[raw_path]
    rollout_file = tmp_path / "sessions/rollout.jsonl"
    host.files[rollout_file] = (
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}, "timestamp": "2026-08-27T12:40:00Z"}\n'
    )
    host.files[tmp_path / TRANSCRIPT_PATH_FILENAME] = str(rollout_file)
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 40, 0, tzinfo=timezone.utc)

    # When reading rollout pointer raises OSError, falls through to next candidate
    host.raise_on_read.add(tmp_path / TRANSCRIPT_PATH_FILENAME)
    common_path = tmp_path / COMMON_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[common_path] = (
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}, "timestamp": "2026-08-27T12:45:00Z"}\n'
    )
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 45, 0, tzinfo=timezone.utc)
    host.raise_on_read.remove(tmp_path / TRANSCRIPT_PATH_FILENAME)
    del host.files[tmp_path / TRANSCRIPT_PATH_FILENAME]
    del host.files[common_path]

    # Fallback to transcript.jsonl
    host.files[tmp_path / "transcript.jsonl"] = (
        '{"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}, "timestamp": "2026-08-27T12:50:00Z"}\n'
    )
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 50, 0, tzinfo=timezone.utc)
    del host.files[tmp_path / "transcript.jsonl"]
    del host.files[tmp_path / IDLE_SINCE_FILENAME]

    # Fallback to session_started / codex_process_started file mtimes
    session_started = tmp_path / "session_started"
    # naive mtime
    host.mtimes[session_started] = datetime(2026, 8, 27, 11, 0, 0)
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 11, 0, 0, tzinfo=timezone.utc)

    proc_started = tmp_path / "codex_process_started"
    # newer tz-aware mtime
    host.mtimes[proc_started] = datetime(2026, 8, 27, 11, 30, 0, tzinfo=timezone.utc)
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 11, 30, 0, tzinfo=timezone.utc)

    # Exception during idle_since check returns None
    host.raise_on_exists = True
    assert get_agent_idle_since(agent) is None
    host.raise_on_exists = False


def test_codex_agent_compaction_capability(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    mock_client = _MockAppServerClient()
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_client=mock_client,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )

    compaction_agent = require_compaction_agent(agent)
    # Default cache TTL for OpenAI / Codex is 30 minutes
    assert compaction_agent.get_cache_ttl_minutes() == CODEX_DEFAULT_CACHE_TTL_MINUTES
    assert compaction_agent.get_cache_ttl_minutes() == 30

    # Initially not idle
    assert compaction_agent.get_idle_since() is None

    # Set idle
    idle_dt = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = idle_dt.isoformat()
    assert compaction_agent.get_idle_since() == idle_dt

    # Set transcript tokens
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = (
        '{"type": "event_msg", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 18500}}}}\n'
    )
    assert compaction_agent.get_context_tokens() == 18500

    # Request compaction (with instructions ignored)
    compaction_agent.request_compaction(instructions="custom instructions should be ignored")
    assert len(mock_client.compact_calls) == 1
    assert mock_client.closed is True

    # Last compacted idle_since is recorded
    last_compacted = get_agent_last_compacted_idle_since(agent)
    assert last_compacted is not None
    assert last_compacted >= idle_dt

    # After compaction, get_idle_since returns None for the same epoch
    assert compaction_agent.get_idle_since() is None

    # When new activity occurs (newer timestamp), get_idle_since returns the new epoch
    new_idle_dt = last_compacted + timedelta(hours=1)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = new_idle_dt.isoformat()
    assert compaction_agent.get_idle_since() == new_idle_dt


def test_codex_agent_request_compaction_failure(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    mock_client = _MockAppServerClient(should_fail=True)
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_client=mock_client,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )

    compaction_agent = require_compaction_agent(agent)
    with pytest.raises(SendMessageError, match="failed to request context compaction"):
        compaction_agent.request_compaction()
    assert mock_client.closed is True
    assert get_agent_last_compacted_idle_since(agent) is not None


def test_codex_agent_post_compaction_transcript_not_idle(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    mock_client = _MockAppServerClient()
    agent = _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_client=mock_client,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )
    compaction_agent = require_compaction_agent(agent)

    # Initial transcript with conversational turn
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:00:00Z", "payload": {"type": "agent_message", "message": "hello"}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:01:00Z", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 60000}}}}\n'
    )
    assert compaction_agent.get_context_tokens() == 60000
    assert compaction_agent.get_idle_since() == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    # Run compaction
    compaction_agent.request_compaction()

    # Now Codex app server appends compaction events with newer timestamp
    host.files[raw_path] += (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:00Z", "payload": {"type": "item_completed", "turn_id": "turn-c", "item": {"type": "ContextCompaction"}}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:01Z", "payload": {"type": "token_count", "info": {"last_token_usage": {"input_tokens": 0, "total_tokens": 5000}, "total_token_usage": {"input_tokens": 65000}}}}\n'
        '{"type": "event_msg", "timestamp": "2026-08-27T12:02:02Z", "payload": {"type": "task_complete", "turn_id": "turn-c", "last_agent_message": null, "completed_at": 1787832122}}\n'
        '{"type": "compacted", "timestamp": "2026-08-27T12:02:03Z", "payload": {"latest_token_usage_record": {"turn_id": "turn-c"}}}\n'
    )

    # Context tokens are reduced to post-compaction count
    assert compaction_agent.get_context_tokens() == 5000

    # Compaction turn does NOT start a new idle epoch; agent is recognized as already compacted
    assert compaction_agent.get_idle_since() is None

    # When a new user turn and assistant response arrive, idle epoch restarts
    host.files[raw_path] += (
        '{"type": "event_msg", "timestamp": "2026-08-27T12:10:00Z", "payload": {"type": "agent_message", "message": "new response"}}\n'
    )
    assert compaction_agent.get_idle_since() == datetime(2026, 8, 27, 12, 10, 0, tzinfo=timezone.utc)


def _reference_latest_assistant_timestamp(raw_text: str) -> datetime | None:
    """A whole-file, newest-first parser of the newest assistant timestamp, kept as an oracle."""
    compaction_turn_ids: set[str] = set()
    for line in reversed(raw_text.strip().splitlines()):
        stripped = line.strip()
        record = json.loads(stripped) if stripped else None
        if not isinstance(record, dict):
            continue
        event_type = record.get("type")
        raw_payload = record.get("payload")
        payload: dict[str, Any] = raw_payload if isinstance(raw_payload, dict) else {}
        if event_type == "compacted":
            if isinstance(raw_payload, dict):
                tur = raw_payload.get("latest_token_usage_record")
                if isinstance(tur, dict) and tur.get("turn_id"):
                    compaction_turn_ids.add(str(tur["turn_id"]))
            continue
        if event_type in ("token_usage_record", "token_count"):
            continue
        if payload.get("type") == "item_completed":
            item = payload.get("item")
            if isinstance(item, dict) and item.get("type") == "ContextCompaction" and payload.get("turn_id"):
                compaction_turn_ids.add(str(payload["turn_id"]))
            continue
        is_assistant_activity = False
        if event_type == "event_msg":
            ptype = payload.get("type")
            if ptype == "agent_message":
                is_assistant_activity = True
            elif ptype == "task_complete":
                turn_id = payload.get("turn_id")
                is_assistant_activity = not (
                    (turn_id and str(turn_id) in compaction_turn_ids)
                    or ("last_agent_message" in payload and payload["last_agent_message"] is None)
                )
            else:
                is_assistant_activity = False
        elif event_type == "response_item":
            role = payload.get("role")
            is_assistant_activity = role == "assistant" or (
                role != "user" and payload.get("type") in ("message", "reasoning", "function_call", "custom_tool_call")
            )
        elif event_type in ("assistant", "assistant_message", "observation"):
            is_assistant_activity = True
        elif event_type == "step":
            is_assistant_activity = record.get("source") in ("agent", "assistant")
        else:
            is_assistant_activity = False
        if is_assistant_activity:
            completed_at = payload.get("completed_at")
            if isinstance(completed_at, (int, float)):
                return datetime.fromtimestamp(completed_at, tz=timezone.utc)
            ts_str = record.get("timestamp") or payload.get("timestamp")
            dt = parse_iso_timestamp(ts_str) if isinstance(ts_str, str) else None
            if dt is not None:
                return dt
    return None


def _reference_context_tokens(raw_text: str) -> int | None:
    """A whole-file, newest-first parser of the context size, kept as an oracle."""
    for line in reversed(raw_text.strip().splitlines()):
        stripped = line.strip()
        record = json.loads(stripped) if stripped else None
        if not isinstance(record, dict):
            continue
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        info = payload.get("info") if payload.get("type") == "token_count" else None
        if not isinstance(info, dict):
            continue
        last_usage = info.get("last_token_usage")
        if isinstance(last_usage, dict):
            for key in ("input_tokens", "total_tokens"):
                value = last_usage.get(key)
                if isinstance(value, int) and value > 0:
                    return value
        total_usage = info.get("total_token_usage")
        if isinstance(total_usage, dict):
            value = total_usage.get("input_tokens")
            if isinstance(value, int) and value > 0:
                return value
    return None


def _rollout(*records: dict[str, Any]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _user_message(minute: int) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": f"2026-08-27T12:{minute:02d}:00.000Z",
        "payload": {"type": "user_message", "message": "please continue"},
    }


def _assistant_message(minute: int) -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": f"2026-08-27T12:{minute:02d}:20.000Z",
        "payload": {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "done"}]},
    }


def _function_call_output(minute: int, output: str) -> dict[str, Any]:
    return {
        "type": "response_item",
        "timestamp": f"2026-08-27T12:{minute:02d}:25.000Z",
        "payload": {"type": "function_call_output", "role": "user", "call_id": f"call_{minute}", "output": output},
    }


def _token_count(minute: int, input_tokens: int) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": f"2026-08-27T12:{minute:02d}:30.000Z",
        "payload": {
            "type": "token_count",
            "info": {
                "last_token_usage": {"input_tokens": input_tokens, "output_tokens": 300},
                "total_token_usage": {"input_tokens": input_tokens * 3, "output_tokens": 900},
            },
        },
    }


def _task_complete(minute: int, turn_id: str, last_agent_message: str | None) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": f"2026-08-27T12:{minute:02d}:40.000Z",
        "payload": {
            "type": "task_complete",
            "turn_id": turn_id,
            "last_agent_message": last_agent_message,
            "completed_at": 1787832000 + minute * 60,
        },
    }


def _compaction_item_completed(minute: int, turn_id: str) -> dict[str, Any]:
    return {
        "type": "event_msg",
        "timestamp": f"2026-08-27T12:{minute:02d}:41.000Z",
        "payload": {"type": "item_completed", "turn_id": turn_id, "item": {"type": "ContextCompaction"}},
    }


def _compacted(minute: int, turn_id: str) -> dict[str, Any]:
    return {
        "type": "compacted",
        "timestamp": f"2026-08-27T12:{minute:02d}:42.000Z",
        "payload": {"latest_token_usage_record": {"turn_id": turn_id}},
    }


_CODEX_ROLLOUT_FIXTURES = {
    "turns_with_large_tool_output_after_the_last_turn": _rollout(
        _user_message(1),
        _assistant_message(1),
        _token_count(1, 41000),
        _task_complete(1, "turn-1", "done"),
        _user_message(2),
        _assistant_message(2),
        _token_count(2, 52000),
        _function_call_output(2, "q" * 150_000),
    ),
    "compaction_turn_after_a_real_turn": _rollout(
        _user_message(1),
        _assistant_message(1),
        _token_count(1, 180000),
        _task_complete(1, "turn-1", "done"),
        _compaction_item_completed(3, "turn-compact"),
        _token_count(3, 6000),
        _task_complete(3, "turn-compact", None),
        _compacted(3, "turn-compact"),
    ),
    "compacted_record_names_the_newest_completed_turn": _rollout(
        _user_message(1),
        _assistant_message(1),
        _token_count(1, 90000),
        _task_complete(1, "turn-1", "done"),
        _compacted(2, "turn-1"),
    ),
    "no_assistant_activity": _rollout(_user_message(1), _token_count(1, 1000)),
}


@pytest.mark.parametrize("fixture_name", sorted(_CODEX_ROLLOUT_FIXTURES))
def test_agent_compaction_values_match_the_whole_file_parsers(
    fixture_name: str, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    rollout = _CODEX_ROLLOUT_FIXTURES[fixture_name]
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = rollout

    assert get_agent_idle_since(agent) == _reference_latest_assistant_timestamp(rollout)
    assert get_agent_context_tokens(agent) == _reference_context_tokens(rollout)


def test_appended_compaction_record_naming_the_cached_turn_is_honored(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = _rollout(
        _user_message(1), _assistant_message(1), _token_count(1, 90000), _task_complete(4, "turn-1", "done")
    )
    before_compaction = get_agent_idle_since(agent)

    host.files[raw_path] += _rollout(_compacted(5, "turn-1"))
    after_compaction = get_agent_idle_since(agent)

    assert before_compaction == datetime.fromtimestamp(1787832000 + 4 * 60, tz=timezone.utc)
    assert after_compaction == _reference_latest_assistant_timestamp(host.files[raw_path])
    assert after_compaction == datetime(2026, 8, 27, 12, 1, 20, tzinfo=timezone.utc)


def test_appended_turn_updates_the_cached_values(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost(host_dir=tmp_path)
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[raw_path] = _rollout(_user_message(1), _assistant_message(1), _token_count(1, 90000))
    get_agent_idle_since(agent)
    get_agent_context_tokens(agent)

    host.files[raw_path] += _rollout(_user_message(7), _assistant_message(7), _token_count(7, 95000))

    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 7, 20, tzinfo=timezone.utc)
    assert get_agent_context_tokens(agent) == 95000


def _make_agent(
    tmp_path: Path,
    mngr_ctx: MngrContext,
    host: _RecordingHost,
    mock_client: _MockAppServerClient | None = None,
) -> _RecordingCodexAgent:
    return _RecordingCodexAgent.model_construct(
        id=AgentId.generate(),
        name=AgentName("test-codex"),
        agent_type=AgentTypeName("codex"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=mngr_ctx,
        agent_config=CodexAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_client=mock_client,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )


_COMPACTION_IDLE_SINCE = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_idle_recording_codex_agent(
    tmp_path: Path, temp_mngr_ctx: MngrContext, mock_client: _MockAppServerClient
) -> _RecordingCodexAgent:
    host = _RecordingHost(host_dir=tmp_path)
    host.files[tmp_path / IDLE_SINCE_FILENAME] = _COMPACTION_IDLE_SINCE.isoformat()
    return _make_agent(tmp_path, temp_mngr_ctx, host, mock_client)


def test_codex_request_compaction_gives_up_on_a_message_lock_another_process_holds(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    mock_client = _MockAppServerClient()
    agent = _make_idle_recording_codex_agent(tmp_path, temp_mngr_ctx, mock_client)

    with file_lock_held_by_another_process(tmp_path / "message.lock", temp_mngr_ctx.concurrency_group):
        with pytest.raises(MessageLockTimeoutError):
            agent.request_compaction(message_lock_timeout_seconds=0.5, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert mock_client.compact_calls == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_codex_request_compaction_skips_an_agent_that_became_active(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    mock_client = _MockAppServerClient()
    agent = _make_idle_recording_codex_agent(tmp_path, temp_mngr_ctx, mock_client)
    agent.mock_lifecycle_state = AgentLifecycleState.RUNNING

    with pytest.raises(AgentNoLongerIdleError):
        agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert mock_client.compact_calls == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_codex_request_compaction_records_the_idle_start_it_compacted(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    mock_client = _MockAppServerClient()
    agent = _make_idle_recording_codex_agent(tmp_path, temp_mngr_ctx, mock_client)

    agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert len(mock_client.compact_calls) == 1
    assert get_agent_last_compacted_idle_since(agent) == _COMPACTION_IDLE_SINCE
