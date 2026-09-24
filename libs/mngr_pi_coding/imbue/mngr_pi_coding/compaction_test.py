from __future__ import annotations

import json
from collections.abc import Callable
from collections.abc import Mapping
from datetime import datetime
from datetime import timezone
from pathlib import Path

from pydantic import ConfigDict
from pydantic import Field

from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.interfaces.agent import AgentLifecycleState
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr_pi_coding.compaction import extract_context_tokens_from_jsonl
from imbue.mngr_pi_coding.compaction import extract_latest_assistant_timestamp_from_jsonl
from imbue.mngr_pi_coding.compaction import get_agent_cache_ttl_minutes
from imbue.mngr_pi_coding.compaction import get_agent_last_compacted_idle_since
from imbue.mngr_pi_coding.compaction import parse_iso_timestamp
from imbue.mngr_pi_coding.compaction import record_agent_compacted
from imbue.mngr_pi_coding.pi_coding_config import ACTIVE_MARKER_NAME
from imbue.mngr_pi_coding.pi_coding_config import COMPACTION_REQUEST_KEY
from imbue.mngr_pi_coding.pi_coding_config import IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import LAST_COMPACTED_IDLE_SINCE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import MODEL_STATE_FILENAME
from imbue.mngr_pi_coding.pi_coding_config import PI_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import PI_OPENAI_CACHE_TTL_MINUTES
from imbue.mngr_pi_coding.pi_coding_config import RAW_TRANSCRIPT_OUTPUT_RELATIVE
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


def test_parse_iso_timestamp() -> None:
    dt = parse_iso_timestamp("2026-08-27T12:00:00Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    dt = parse_iso_timestamp("2026-08-27T12:00:00+00:00")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, tzinfo=timezone.utc)

    dt = parse_iso_timestamp("2026-08-27T12:00:00.123456Z")
    assert dt == datetime(2026, 8, 27, 12, 0, 0, 123456, tzinfo=timezone.utc)

    assert parse_iso_timestamp("invalid-date") is None
    assert parse_iso_timestamp("") is None


def test_extract_latest_assistant_timestamp_from_jsonl() -> None:
    assert extract_latest_assistant_timestamp_from_jsonl("") is None
    assert extract_latest_assistant_timestamp_from_jsonl("not json\n") is None

    # Multi-turn events
    raw_transcript = (
        '{"type": "message", "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "user", "content": "hello"}}\n'
        '{"type": "message", "timestamp": "2026-08-27T12:01:00Z", "message": {"role": "assistant", "content": "hi"}}\n'
    )
    assert extract_latest_assistant_timestamp_from_jsonl(raw_transcript) == datetime(
        2026, 8, 27, 12, 1, 0, tzinfo=timezone.utc
    )

    # Millisecond timestamp
    ms_transcript = (
        '{"type": "message", "timestamp": 1787832060000, "message": {"role": "assistant", "content": "hi"}}\n'
    )
    assert extract_latest_assistant_timestamp_from_jsonl(ms_transcript) == datetime.fromtimestamp(
        1787832060, tz=timezone.utc
    )

    # Compaction event alone is not assistant activity
    compaction_transcript = (
        '{"type": "compaction", "timestamp": "2026-08-27T12:02:00Z", "entry": {"summary": "done"}}\n'
    )
    assert extract_latest_assistant_timestamp_from_jsonl(compaction_transcript) is None

    # User message alone is not assistant activity
    user_transcript = '{"type": "message", "timestamp": "2026-08-27T12:00:00Z", "message": {"role": "user"}}\n'
    assert extract_latest_assistant_timestamp_from_jsonl(user_transcript) is None


def test_extract_context_tokens_from_jsonl() -> None:
    assert extract_context_tokens_from_jsonl("") is None
    assert extract_context_tokens_from_jsonl("invalid json") is None

    # Raw assistant message with usage (input + cacheRead)
    raw = '{"type": "message", "message": {"role": "assistant", "usage": {"input": 1000, "cacheRead": 500, "cacheWrite": 250, "output": 200}}}\n'
    assert extract_context_tokens_from_jsonl(raw) == 1750

    # Cost snapshot with tokens (input + cache_read + cache_creation)
    cost_snap = '{"type": "cost_snapshot", "tokens": {"input": 2000, "cache_read": 300, "cache_creation": 150, "output": 100}}\n'
    assert extract_context_tokens_from_jsonl(cost_snap) == 2450

    # Common transcript ATIF step metrics
    step = '{"type": "step", "source": "agent", "metrics": {"prompt_tokens": 4000}}\n'
    assert extract_context_tokens_from_jsonl(step) == 4000

    # Compaction entry tokensBefore
    compaction = '{"type": "compaction", "entry": {"tokensBefore": 8500, "summary": "compacted"}}\n'
    assert extract_context_tokens_from_jsonl(compaction) == 8500


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
