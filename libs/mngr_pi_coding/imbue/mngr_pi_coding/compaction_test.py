from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any

import pytest
from loguru import logger
from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.agents.jsonl_backward_scan import scan_jsonl_file_backward
from imbue.mngr.agents.mock_host_file_read_test import InMemoryHostFileReader
from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.errors import AgentNoLongerIdleError
from imbue.mngr.errors import MessageLockTimeoutError
from imbue.mngr.interfaces.agent import AgentLifecycleState
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.interfaces.data_types import FileTailRead
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr.utils.testing import capture_loguru
from imbue.mngr.utils.testing import file_lock_held_by_another_process
from imbue.mngr_pi_coding.compaction import PiCompactionTranscriptScanner
from imbue.mngr_pi_coding.compaction import get_agent_cache_ttl_minutes
from imbue.mngr_pi_coding.compaction import get_agent_context_tokens
from imbue.mngr_pi_coding.compaction import get_agent_idle_since
from imbue.mngr_pi_coding.compaction import get_agent_last_compacted_idle_since
from imbue.mngr_pi_coding.compaction import parse_iso_timestamp
from imbue.mngr_pi_coding.compaction import record_agent_compacted
from imbue.mngr_pi_coding.pi_coding_config import ACTIVE_MARKER_NAME
from imbue.mngr_pi_coding.pi_coding_config import COMMON_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_pi_coding.pi_coding_config import COMPACTION_REQUEST_KEY
from imbue.mngr_pi_coding.pi_coding_config import IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import LAST_COMPACTED_IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import MODEL_STATE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import PI_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import PI_OPENAI_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import RAW_TRANSCRIPT_OUTPUT_RELATIVE
from imbue.mngr_pi_coding.pi_coding_config import SESSION_POINTER_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import USAGE_OUTPUT_RELATIVE
from imbue.mngr_pi_coding.plugin import PiCodingAgent
from imbue.mngr_pi_coding.plugin import PiCodingAgentConfig


class _RecordingHost(FakeHost):
    """Host test double that records text files and checks."""

    files: dict[Path, str] = Field(default_factory=dict)
    mtimes: dict[Path, datetime] = Field(default_factory=dict)
    executed_commands: list[str] = Field(default_factory=list)

    def path_exists(self, path: Path) -> bool:
        return Path(path) in self.files or Path(path) in self.mtimes

    def read_text_file(self, path: Path, encoding: str = "utf-8") -> str:
        return self.files[Path(path)]

    def read_file_tail_from_offset(self, path: Path, start_byte: int) -> FileTailRead:
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

    def execute_stateful_command(
        self,
        command: str,
        user: str | None = None,
        cwd: Path | None = None,
        env: Mapping[str, str] | None = None,
        timeout_seconds: float | None = None,
        on_output: Callable[[str, bool], None] | None = None,
    ) -> CommandResult:
        self.executed_commands.append(command)
        return CommandResult(
            success=True,
            exit_code=0,
            stdout="",
            stderr="",
        )


class _RecordingPiAgent(PiCodingAgent):
    """PiCodingAgent test double for compaction testing."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    override_agent_dir: Path = Field(default_factory=Path)
    mock_lifecycle_state: AgentLifecycleState = AgentLifecycleState.WAITING

    def _get_agent_dir(self) -> Path:
        return self.override_agent_dir

    def get_lifecycle_state(self) -> AgentLifecycleState:
        return self.mock_lifecycle_state


def _scan_text(raw_text: str) -> PiCompactionTranscriptScanner:
    path = Path("/agent/session.jsonl")
    scanner = PiCompactionTranscriptScanner()
    scan_jsonl_file_backward(InMemoryHostFileReader(contents_by_path={path: raw_text.encode("utf-8")}), path, scanner)
    return scanner


def _latest_assistant_timestamp(raw_text: str) -> datetime | None:
    return _scan_text(raw_text).latest_assistant_timestamp


def _latest_context_tokens(raw_text: str) -> int | None:
    return _scan_text(raw_text).latest_context_tokens


def test_parse_iso_timestamp() -> None:
    dt = parse_iso_timestamp("2026-08-27T12:00:00Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    dt = parse_iso_timestamp("2026-08-27T12:00:00+00:00")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    dt = parse_iso_timestamp("2026-08-27T12:00:00.123456Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, 123456, tzinfo=timezone.utc)

    assert parse_iso_timestamp("invalid-date") is None
    assert parse_iso_timestamp("") is None


def test_scanner_finds_latest_assistant_timestamp() -> None:
    assert _latest_assistant_timestamp("") is None
    assert _latest_assistant_timestamp("not json\n") is None

    # Multi-turn events
    raw_transcript = (
        '{"type": "message", "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "user", "content": "hello"}}\n'
        '{"type": "message", "timestamp": "2026-08-27T12:01:00Z", "message": {"role": "assistant", "content": "hi"}}\n'
    )
    assert _latest_assistant_timestamp(raw_transcript) == datetime(2026, 8, 27, 12, 1, 0, tzinfo=timezone.utc)

    # Millisecond timestamp
    ms_transcript = (
        '{"type": "message", "timestamp": 1787832060000, "message": {"role": "assistant", "content": "hi"}}\n'
    )
    assert _latest_assistant_timestamp(ms_transcript) == datetime.fromtimestamp(1787832060, tz=timezone.utc)

    # Compaction event alone is not assistant activity
    compaction_transcript = (
        '{"type": "compaction", "timestamp": "2026-08-27T12:02:00Z", "entry": {"summary": "done"}}\n'
    )
    assert _latest_assistant_timestamp(compaction_transcript) is None

    # User message alone is not assistant activity
    user_transcript = '{"type": "message", "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "user"}}\n'
    assert _latest_assistant_timestamp(user_transcript) is None


def test_scanner_finds_latest_context_tokens() -> None:
    assert _latest_context_tokens("") is None
    assert _latest_context_tokens("invalid json") is None

    # Raw assistant message with usage (input + cacheRead)
    raw = '{"type": "message", "message": {"role": "assistant", "usage": {"input": 1000, "cacheRead": 500, "cacheWrite": 250, "output": 200}}}\n'
    assert _latest_context_tokens(raw) == 1750

    # Cost snapshot with tokens (input + cache_read + cache_creation)
    cost_snap = '{"type": "cost_snapshot", "tokens": {"input": 2000, "cache_read": 300, "cache_creation": 150, "output": 100}}\n'
    assert _latest_context_tokens(cost_snap) == 2450

    # Common transcript ATIF step metrics
    step = '{"type": "step", "source": "agent", "metrics": {"prompt_tokens": 4000}}\n'
    assert _latest_context_tokens(step) == 4000

    # A compaction's own token counts describe the context before it, so they are not a context size
    compaction = '{"type": "compaction", "entry": {"tokensBefore": 8500, "summary": "compacted"}}\n'
    assert _latest_context_tokens(compaction) is None


def test_get_agent_cache_ttl_minutes(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost()
    agent = _RecordingPiAgent.model_construct(
        name=AgentName("test-pi"),
        id=AgentId.generate(),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=PiCodingAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    # Default without model_state.json
    assert get_agent_cache_ttl_minutes(agent) == PI_DEFAULT_CACHE_TTL_MINUTES

    # Non-OpenAI model falls back to default
    model_state_path = tmp_path / MODEL_STATE_FILENAME
    host.files[model_state_path] = json.dumps({"model": "anthropic/claude-3-5-sonnet"})
    assert get_agent_cache_ttl_minutes(agent) == PI_DEFAULT_CACHE_TTL_MINUTES

    # OpenAI model
    host.files[model_state_path] = json.dumps({"model": "openai/gpt-4o"})
    assert get_agent_cache_ttl_minutes(agent) == PI_OPENAI_CACHE_TTL_MINUTES


def test_record_agent_compacted(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost()
    agent = _RecordingPiAgent.model_construct(
        name=AgentName("test-pi"),
        id=AgentId.generate(),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=PiCodingAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
    )

    dt = datetime(2026, 8, 27, 12, 30, 0, tzinfo=timezone.utc)
    record_agent_compacted(agent, idle_since=dt)

    assert get_agent_last_compacted_idle_since(agent) == dt
    assert host.files[tmp_path / LAST_COMPACTED_IDLE_SINCE_FILENAME] == dt.isoformat()
    assert host.files[tmp_path / IDLE_SINCE_FILENAME] == dt.isoformat()


def test_get_agent_idle_since_and_compaction_cycle(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost()
    agent = _RecordingPiAgent.model_construct(
        name=AgentName("test-pi"),
        id=AgentId.generate(),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=temp_mngr_ctx,
        agent_config=PiCodingAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )
    compaction_agent = require_compaction_agent(agent)

    # Initial transcript with turn
    raw_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    turn1_time = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)
    host.files[raw_path] = (
        '{"type": "message", "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "assistant", "usage": {"input": 45000, "cacheRead": 5000}}}\n'
    )

    assert compaction_agent.get_context_tokens() == 50000
    assert compaction_agent.get_idle_since() == turn1_time

    # While running (active marker present), get_idle_since returns None
    active_path = tmp_path / ACTIVE_MARKER_NAME
    host.files[active_path] = ""
    assert compaction_agent.get_idle_since() is None
    del host.files[active_path]

    # Run compaction
    compaction_agent.request_compaction(instructions="preserve summary")

    # Verify inbox append command recorded
    assert len(host.executed_commands) == 1
    assert COMPACTION_REQUEST_KEY in host.executed_commands[0]
    assert "preserve summary" in host.executed_commands[0]

    # After compaction request, agent is marked compacted for turn1_time
    assert compaction_agent.get_idle_since() is None

    # Lifecycle extension emits compaction record
    host.files[raw_path] += (
        '{"type": "compaction", "timestamp": "2026-08-27T12:01:00Z", "entry": {"tokensBefore": 50000, "summary": "compacted"}}\n'
    )

    # Still recognized as already compacted for this idle epoch
    assert compaction_agent.get_idle_since() is None

    # New turn arrives from user and assistant
    turn2_time = datetime(2026, 8, 27, 12, 10, 0, tzinfo=timezone.utc)
    host.files[raw_path] += (
        '{"type": "message", "timestamp": "2026-08-27T12:10:00Z", "message": {"role": "assistant", "usage": {"input": 8000, "cacheRead": 2000}}}\n'
    )

    # New idle epoch started
    assert compaction_agent.get_idle_since() == turn2_time
    assert compaction_agent.get_context_tokens() == 10000


def _reference_latest_assistant_timestamp(raw_text: str) -> datetime | None:
    """A whole-file, newest-first parser of the newest assistant timestamp, kept as an oracle."""
    if not raw_text:
        return None
    lines = raw_text.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Failed to parse JSONL line when extracting assistant timestamp: {}", e)
            continue
        if not isinstance(record, dict):
            continue

        is_assistant_activity = False
        ts_val: Any = None

        event_type = record.get("type")
        msg = record.get("message")
        if isinstance(msg, dict):
            if msg.get("role") == "assistant":
                is_assistant_activity = True
                ts_val = record.get("timestamp") or msg.get("timestamp")
        elif event_type in ("assistant", "assistant_message"):
            is_assistant_activity = True
            ts_val = record.get("timestamp")
        elif event_type == "step":
            if record.get("source") in ("agent", "assistant"):
                is_assistant_activity = True
                ts_val = record.get("timestamp")
        elif event_type == "cost_snapshot":
            event_id = str(record.get("event_id", ""))
            if "compaction" not in event_id:
                is_assistant_activity = True
                ts_val = record.get("timestamp")
        else:
            pass

        if is_assistant_activity and ts_val is not None:
            if isinstance(ts_val, (int, float)):
                try:
                    # In Pi, timestamps in milliseconds (e.g. 1700000000000) vs seconds
                    if ts_val > 1e11:
                        ts_val = ts_val / 1000.0
                    return datetime.fromtimestamp(ts_val, tz=timezone.utc)
                except (ValueError, OverflowError, OSError):
                    pass
            elif isinstance(ts_val, str):
                dt = parse_iso_timestamp(ts_val)
                if dt is not None:
                    return dt
            else:
                pass

    return None


def _reference_context_tokens(raw_text: str) -> int | None:
    """A whole-file, newest-first parser of the context size, kept as an oracle.

    A compaction newer than any usage leaves the size unknown.
    """
    if not raw_text:
        return None
    lines = raw_text.strip().splitlines()
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("Failed parsing line in JSONL transcript: {}", e)
            continue
        if not isinstance(record, dict):
            continue

        # 0. A compaction newer than any usage leaves the context size unknown
        event_type = record.get("type")
        extra = record.get("extra")
        context_management = extra.get("context_management") if isinstance(extra, dict) else None
        if (
            event_type == "compaction"
            or (event_type == "cost_snapshot" and "compaction" in str(record.get("event_id", "")))
            or (isinstance(context_management, dict) and context_management.get("type") == "compaction")
        ):
            return None

        # 1. Check raw assistant message usage (raw transcript or native session)
        msg = record.get("message")
        if isinstance(msg, dict):
            usage = msg.get("usage")
            if isinstance(usage, dict):
                inp = usage.get("input") or usage.get("input_tokens") or 0
                cached = usage.get("cacheRead") or usage.get("cache_read") or usage.get("cache_read_input_tokens") or 0
                cache_write = (
                    usage.get("cacheWrite")
                    or usage.get("cache_creation")
                    or usage.get("cache_creation_input_tokens")
                    or 0
                )
                if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_write, int):
                    total = inp + cached + cache_write
                    if total > 0:
                        return total
                total_tokens = usage.get("totalTokens") or usage.get("total_tokens")
                output = usage.get("output") or usage.get("output_tokens") or 0
                if isinstance(total_tokens, int) and isinstance(output, int) and total_tokens > output:
                    return total_tokens - output

        # 2. Check cost_snapshot tokens dict (usage stream)
        tokens = record.get("tokens")
        if isinstance(tokens, dict):
            inp = tokens.get("input") or 0
            cached = tokens.get("cache_read") or 0
            cache_creation = tokens.get("cache_creation") or 0
            if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_creation, int):
                total = inp + cached + cache_creation
                if total > 0:
                    return total

        # 3. Check common transcript metrics (ATIF step)
        metrics = record.get("metrics")
        if isinstance(metrics, dict):
            prompt_tokens = metrics.get("prompt_tokens")
            if isinstance(prompt_tokens, int) and prompt_tokens > 0:
                return prompt_tokens

        # 4. Check generic usage dict in record or payload
        payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
        for candidate in (record.get("usage"), payload.get("usage")):
            if isinstance(candidate, dict):
                inp = candidate.get("input") or candidate.get("input_tokens") or 0
                cached = (
                    candidate.get("cacheRead")
                    or candidate.get("cache_read")
                    or candidate.get("cache_read_input_tokens")
                    or 0
                )
                cache_write = (
                    candidate.get("cacheWrite")
                    or candidate.get("cache_creation")
                    or candidate.get("cache_creation_input_tokens")
                    or 0
                )
                if isinstance(inp, int) and isinstance(cached, int) and isinstance(cache_write, int):
                    total = inp + cached + cache_write
                    if total > 0:
                        return total

    return None


def _pi_jsonl(*records: dict[str, Any]) -> str:
    return "".join(json.dumps(record) + "\n" for record in records)


def _pi_user_message(minute: int) -> dict[str, Any]:
    return {
        "type": "message",
        "timestamp": f"2026-08-27T12:{minute:02d}:00.000Z",
        "message": {"role": "user", "content": [{"type": "text", "text": "continue"}]},
    }


def _pi_assistant_message(minute: int, input_tokens: int, cache_read: int) -> dict[str, Any]:
    return {
        "type": "message",
        "timestamp": f"2026-08-27T12:{minute:02d}:20.000Z",
        "message": {
            "role": "assistant",
            "content": [{"type": "toolCall", "name": "bash", "arguments": {"command": "ls"}}],
            "usage": {"input": input_tokens, "cacheRead": cache_read, "cacheWrite": 0, "output": 120},
        },
    }


def _pi_tool_result(minute: int, output: str) -> dict[str, Any]:
    return {
        "type": "message",
        "timestamp": f"2026-08-27T12:{minute:02d}:30.000Z",
        "message": {"role": "toolResult", "content": [{"type": "text", "text": output}]},
    }


def _pi_cost_snapshot(minute: int, event_id: str, input_tokens: int) -> dict[str, Any]:
    return {
        "type": "cost_snapshot",
        "event_id": event_id,
        "timestamp": f"2026-08-27T12:{minute:02d}:40.000Z",
        "tokens": {"input": input_tokens, "cache_read": 1000, "cache_creation": 0, "output": 50},
    }


def _pi_compaction(minute: int, tokens_before: int) -> dict[str, Any]:
    return {
        "type": "compaction",
        "timestamp": f"2026-08-27T12:{minute:02d}:50.000Z",
        "entry": {"tokensBefore": tokens_before, "summary": "compacted"},
    }


_PI_TRANSCRIPT_FIXTURES = {
    "tool_results_after_the_last_assistant_message": _pi_jsonl(
        _pi_user_message(1),
        _pi_assistant_message(1, 40000, 5000),
        _pi_tool_result(1, "ok"),
        _pi_assistant_message(2, 41000, 6000),
        _pi_tool_result(2, "r" * 150_000),
    ),
    "compaction_after_the_last_assistant_message": _pi_jsonl(
        _pi_user_message(1),
        _pi_assistant_message(1, 90000, 5000),
        _pi_compaction(3, 95000),
    ),
    "usage_stream_with_compaction_snapshot": _pi_jsonl(
        _pi_cost_snapshot(1, "turn-1", 30000),
        _pi_cost_snapshot(2, "turn-2", 35000),
        _pi_cost_snapshot(3, "compaction-1", 2000),
    ),
    "no_assistant_activity": _pi_jsonl(_pi_user_message(1), _pi_tool_result(1, "nothing")),
}


@pytest.mark.parametrize("fixture_name", sorted(_PI_TRANSCRIPT_FIXTURES))
def test_agent_compaction_values_match_the_whole_file_parsers(
    fixture_name: str, tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    transcript = _PI_TRANSCRIPT_FIXTURES[fixture_name]
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = transcript

    assert get_agent_idle_since(agent) == _reference_latest_assistant_timestamp(transcript)
    assert get_agent_context_tokens(agent) == _reference_context_tokens(transcript)


def test_agent_values_follow_each_value_s_own_file_order(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    # The idle timestamp prefers the common transcript; the token count prefers the raw transcript
    host.files[tmp_path / COMMON_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(
        {"type": "step", "source": "agent", "timestamp": "2026-08-27T12:09:00Z", "metrics": {"prompt_tokens": 1111}}
    )
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(_pi_assistant_message(5, 20000, 2000))
    session_path = tmp_path / "sessions/session.jsonl"
    host.files[session_path] = _pi_jsonl(_pi_assistant_message(7, 70000, 7000))
    host.files[tmp_path / SESSION_POINTER_FILENAME] = str(session_path)
    host.files[tmp_path / USAGE_OUTPUT_RELATIVE] = _pi_jsonl(_pi_cost_snapshot(8, "turn-8", 80000))

    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 9, 0, tzinfo=timezone.utc)
    assert get_agent_context_tokens(agent) == 22000


def _make_agent(tmp_path: Path, mngr_ctx: MngrContext, host: _RecordingHost) -> _RecordingPiAgent:
    return _RecordingPiAgent.model_construct(
        name=AgentName("test-pi"),
        id=AgentId.generate(),
        agent_type=AgentTypeName("pi-coding"),
        work_dir=tmp_path,
        create_time=datetime.now(timezone.utc),
        host_id=HostId.generate(),
        mngr_ctx=mngr_ctx,
        agent_config=PiCodingAgentConfig(check_installation=False),
        host=host,
        override_agent_dir=tmp_path,
        mock_lifecycle_state=AgentLifecycleState.WAITING,
    )


_COMPACTION_IDLE_SINCE = datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)


def _make_idle_recording_pi_agent(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> tuple[_RecordingHost, _RecordingPiAgent]:
    host = _RecordingHost()
    host.files[tmp_path / IDLE_SINCE_FILENAME] = _COMPACTION_IDLE_SINCE.isoformat()
    return host, _make_agent(tmp_path, temp_mngr_ctx, host)


def test_pi_request_compaction_gives_up_on_a_message_lock_another_process_holds(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host, agent = _make_idle_recording_pi_agent(tmp_path, temp_mngr_ctx)

    with file_lock_held_by_another_process(tmp_path / "message.lock", temp_mngr_ctx.concurrency_group):
        with pytest.raises(MessageLockTimeoutError):
            agent.request_compaction(message_lock_timeout_seconds=0.5, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert host.executed_commands == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_pi_request_compaction_skips_an_agent_that_became_active(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    host, agent = _make_idle_recording_pi_agent(tmp_path, temp_mngr_ctx)
    host.files[tmp_path / ACTIVE_MARKER_NAME] = ""

    with pytest.raises(AgentNoLongerIdleError):
        agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert host.executed_commands == []
    assert get_agent_last_compacted_idle_since(agent) is None


def test_pi_request_compaction_inboxes_the_request_when_still_idle_since_the_expected_moment(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host, agent = _make_idle_recording_pi_agent(tmp_path, temp_mngr_ctx)

    agent.request_compaction(message_lock_timeout_seconds=5.0, expected_idle_since=_COMPACTION_IDLE_SINCE)

    assert len(host.executed_commands) == 1
    assert COMPACTION_REQUEST_KEY in host.executed_commands[0]
    assert get_agent_last_compacted_idle_since(agent) == _COMPACTION_IDLE_SINCE


def _pi_assistant_message_with_context(minute: int, context_tokens: int) -> dict[str, Any]:
    return _pi_assistant_message(minute, 6, context_tokens - 6)


def _pi_compaction_records(minute: int, tokens_before: int) -> list[dict[str, Any]]:
    """The compaction record the lifecycle extension writes, stamped later than the reply written after it."""
    return [
        {
            "type": "compaction",
            "timestamp": f"2026-08-27T12:{minute:02d}:50.000Z",
            "entry": {
                "type": "compaction",
                "id": f"cmp{minute:05d}",
                "parentId": f"msg{minute:05d}",
                "timestamp": f"2026-08-27T12:{minute:02d}:50.000Z",
                "summary": "The user asked for the tests to be fixed; two remain failing.",
                "firstKeptEntryId": f"msg{minute:05d}",
                "tokensBefore": tokens_before,
            },
        }
    ]


def _pi_compacted_transcript_records() -> list[dict[str, Any]]:
    """A 210k-token reply, then a compaction that no reply has followed yet."""
    return [_pi_user_message(1), _pi_assistant_message_with_context(1, 210000), *_pi_compaction_records(5, 210000)]


def test_pi_context_size_is_unknown_after_a_compaction_until_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(*_pi_compacted_transcript_records())

    assert get_agent_context_tokens(agent) is None
    assert get_agent_idle_since(agent) == datetime(2026, 8, 27, 12, 1, 20, tzinfo=timezone.utc)


def test_pi_context_size_after_a_compaction_comes_from_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(
        *_pi_compacted_transcript_records(), _pi_user_message(6), _pi_assistant_message_with_context(6, 30000)
    )

    assert get_agent_context_tokens(agent) == 30000


def test_pi_context_size_stays_unknown_across_two_compactions_with_no_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(
        *_pi_compacted_transcript_records(), *_pi_compaction_records(8, 15000)
    )

    assert get_agent_context_tokens(agent) is None


def test_pi_compaction_is_ordered_by_file_position_not_timestamp(tmp_path: Path, temp_mngr_ctx: MngrContext) -> None:
    reply_host = _RecordingHost()
    reply_agent = _make_agent(tmp_path / "reply", temp_mngr_ctx, reply_host)
    compaction_host = _RecordingHost()
    compaction_agent = _make_agent(tmp_path / "compaction", temp_mngr_ctx, compaction_host)
    # A reply written after the compaction counts even though its timestamp is earlier than the compaction's
    reply_host.files[tmp_path / "reply" / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(
        _pi_assistant_message_with_context(1, 210000),
        *_pi_compaction_records(9, 210000),
        _pi_assistant_message_with_context(8, 30000),
    )
    # A compaction written after a reply hides it even though its timestamp is earlier than the reply's
    compaction_host.files[tmp_path / "compaction" / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(
        _pi_assistant_message_with_context(9, 210000), *_pi_compaction_records(2, 210000)
    )

    assert get_agent_context_tokens(reply_agent) == 30000
    assert get_agent_context_tokens(compaction_agent) is None


def test_pi_compaction_in_a_session_file_usage_stream_or_common_transcript_leaves_the_size_unknown() -> None:
    session_file_compaction = _pi_jsonl(
        _pi_assistant_message_with_context(1, 210000),
        {"type": "compaction", "id": "cmp1", "timestamp": "2026-08-27T12:05:00.000Z", "tokensBefore": 210000},
    )
    usage_stream_compaction = _pi_jsonl(
        _pi_cost_snapshot(1, "evt-pi-usage-turn-1", 200000),
        _pi_cost_snapshot(5, "evt-pi-usage-compaction-1", 209000),
    )
    common_transcript_compaction = _pi_jsonl(
        {"type": "step", "source": "agent", "timestamp": "2026-08-27T12:01:00Z", "metrics": {"prompt_tokens": 210000}},
        {
            "type": "step",
            "source": "system",
            "timestamp": "2026-08-27T12:05:00Z",
            "message": "",
            "extra": {"context_management": {"type": "compaction", "boundary": "replace"}},
        },
    )

    assert _scan_text(session_file_compaction).is_context_size_unknown_since_compaction
    assert _latest_context_tokens(session_file_compaction) is None
    assert _latest_context_tokens(usage_stream_compaction) is None
    assert _latest_context_tokens(common_transcript_compaction) is None


def test_pi_appended_compaction_replaces_a_cached_context_size_until_the_next_reply(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    transcript_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[transcript_path] = _pi_jsonl(_pi_user_message(1), _pi_assistant_message_with_context(1, 210000))
    before_compaction = agent.get_context_tokens()

    host.files[transcript_path] += _pi_jsonl(*_pi_compaction_records(5, 210000))
    after_compaction = agent.get_context_tokens()
    host.files[transcript_path] += _pi_jsonl(_pi_user_message(6), _pi_assistant_message_with_context(6, 30000))
    after_reply = agent.get_context_tokens()

    assert before_compaction == 210000
    assert after_compaction is None
    assert after_reply == 30000


def test_pi_suppressed_context_size_is_logged_at_debug_with_the_agent_and_transcript(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    transcript_path = tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE
    host.files[transcript_path] = _pi_jsonl(*_pi_compacted_transcript_records())

    with capture_loguru(level="DEBUG") as log_output:
        get_agent_context_tokens(agent)

    assert (
        f"Context size of agent test-pi is unknown until its next reply: {transcript_path} has a compaction"
        in log_output.getvalue()
    )


def test_pi_compaction_in_the_raw_transcript_is_not_overridden_by_the_session_file(
    tmp_path: Path, temp_mngr_ctx: MngrContext
) -> None:
    host = _RecordingHost()
    agent = _make_agent(tmp_path, temp_mngr_ctx, host)
    host.files[tmp_path / RAW_TRANSCRIPT_OUTPUT_RELATIVE] = _pi_jsonl(*_pi_compacted_transcript_records())
    session_path = tmp_path / "sessions/session.jsonl"
    host.files[session_path] = _pi_jsonl(_pi_assistant_message_with_context(1, 210000))
    host.files[tmp_path / SESSION_POINTER_FILENAME] = str(session_path)

    assert get_agent_context_tokens(agent) is None
