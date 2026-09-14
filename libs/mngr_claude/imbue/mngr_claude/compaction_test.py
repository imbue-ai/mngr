from datetime import datetime
from datetime import timedelta
from datetime import timezone
from pathlib import Path

from pydantic import Field

from imbue.mngr.api.testing import FakeHost
from imbue.mngr.config.data_types import MngrContext
from imbue.mngr.hosts.tmux import TmuxWindowTarget
from imbue.mngr.interfaces.agent import require_compaction_agent
from imbue.mngr.interfaces.data_types import CommandResult
from imbue.mngr.primitives import AgentId
from imbue.mngr.primitives import AgentName
from imbue.mngr.primitives import AgentTypeName
from imbue.mngr.primitives import HostId
from imbue.mngr_claude.claude_config import IDLE_SINCE_FILENAME
from imbue.mngr_claude.compaction import CLAUDE_DEFAULT_CACHE_TTL_MINUTES
from imbue.mngr_claude.compaction import extract_context_tokens_from_jsonl
from imbue.mngr_claude.compaction import extract_latest_assistant_timestamp_from_jsonl
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

    def _warn_if_preexisting_input_text(self, tmux_target: TmuxWindowTarget) -> None:
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


def test_extract_latest_assistant_timestamp_from_jsonl() -> None:
    assert extract_latest_assistant_timestamp_from_jsonl("") is None
    assert extract_latest_assistant_timestamp_from_jsonl("not json\n") is None

    # Multi-turn transcript
    raw_transcript = (
        '{"type": "user", "timestamp": "2026-08-27T12:00:00Z", "message": {"content": "hello"}}\n'
        '{"type": "assistant", "timestamp": "2026-08-27T12:01:00Z", "message": {"usage": {"input_tokens": 100}}}\n'
        '{"type": "user", "timestamp": "2026-08-27T12:05:00Z", "text": "/compact"}\n'
    )
    # Returns the timestamp of the assistant turn (not the /compact command)
    assert extract_latest_assistant_timestamp_from_jsonl(raw_transcript) == datetime(
        2026, 8, 27, 12, 1, 0, tzinfo=timezone.utc
    )


def test_extract_context_tokens_from_jsonl() -> None:
    assert extract_context_tokens_from_jsonl("") is None
    assert extract_context_tokens_from_jsonl("not json\n") is None

    # Raw transcript format with caching
    raw_transcript = (
        '{"type": "user", "message": {"content": "hello"}}\n'
        '{"type": "assistant", "message": {"model": "claude-opus-4-8", "usage": {"input_tokens": 2, "cache_creation_input_tokens": 1500, "cache_read_input_tokens": 120000, "output_tokens": 100}}}\n'
    )
    assert extract_context_tokens_from_jsonl(raw_transcript) == 121502

    # Multiple assistant messages -> returns latest
    multi_turn = (
        '{"type": "assistant", "message": {"usage": {"input_tokens": 50000}}}\n'
        '{"type": "user", "message": {"content": "more"}}\n'
        '{"type": "assistant", "message": {"usage": {"input_tokens": 10, "cache_read_input_tokens": 80000, "cache_creation_input_tokens": 2000}}}\n'
    )
    assert extract_context_tokens_from_jsonl(multi_turn) == 82010

    # Common transcript format
    common_transcript = (
        '{"type": "user_message", "text": "do something"}\n'
        '{"type": "assistant_message", "usage": {"input_tokens": 5, "cache_read_tokens": 90000, "cache_write_tokens": 1000, "output_tokens": 50}}\n'
    )
    assert extract_context_tokens_from_jsonl(common_transcript) == 91005


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
