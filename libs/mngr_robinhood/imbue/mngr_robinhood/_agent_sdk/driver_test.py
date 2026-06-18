import json
from pathlib import Path
from typing import Any

from claude_agent_sdk import AssistantMessage
from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk import Message
from claude_agent_sdk import StreamEvent

from imbue.mngr.api.events import EventsTarget
from imbue.mngr.hosts.host import Host
from imbue.mngr_claude.stream_buffer import SnapshotDeltaReader
from imbue.mngr_robinhood._agent_sdk.driver import LiveSession
from imbue.mngr_robinhood._agent_sdk.driver import _TurnDrainTicker
from imbue.mngr_robinhood._agent_sdk.driver import _build_agent_name
from imbue.mngr_robinhood._agent_sdk.driver import _build_environment
from imbue.mngr_robinhood._agent_sdk.driver import _options_with_overrides
from imbue.mngr_robinhood._agent_sdk.driver import _system_prompt_args
from imbue.mngr_robinhood._agent_sdk.driver import map_options_to_agent_args
from imbue.mngr_robinhood._agent_sdk.driver import resolve_cwd
from imbue.mngr_robinhood._agent_sdk.stream_events import StreamEventSynthesizer


def test_map_options_minimal_is_empty() -> None:
    assert map_options_to_agent_args(ClaudeAgentOptions()) == ()


def test_map_options_model_and_permission_mode() -> None:
    args = map_options_to_agent_args(ClaudeAgentOptions(model="haiku", permission_mode="bypassPermissions"))
    assert "--model" in args
    assert args[args.index("--model") + 1] == "haiku"
    assert "--permission-mode" in args
    assert args[args.index("--permission-mode") + 1] == "bypassPermissions"


def test_map_options_allowed_and_disallowed_tools_use_camelcase_flags() -> None:
    args = map_options_to_agent_args(ClaudeAgentOptions(allowed_tools=["Bash", "Read"], disallowed_tools=["WebFetch"]))
    assert "--allowedTools" in args
    assert args[args.index("--allowedTools") + 1] == "Bash,Read"
    assert "--disallowedTools" in args
    assert args[args.index("--disallowedTools") + 1] == "WebFetch"


def test_map_options_add_dirs_are_repeated() -> None:
    args = map_options_to_agent_args(ClaudeAgentOptions(add_dirs=["/a", "/b"]))
    assert args.count("--add-dir") == 2
    assert "/a" in args and "/b" in args


def test_map_options_max_turns_and_settings() -> None:
    args = map_options_to_agent_args(ClaudeAgentOptions(max_turns=3, settings="/tmp/settings.json"))
    assert args[args.index("--max-turns") + 1] == "3"
    assert args[args.index("--settings") + 1] == "/tmp/settings.json"


def test_map_options_does_not_emit_resume_continue_fork_or_setting_sources() -> None:
    # These are handled by agent reuse / raise, never translated to claude flags.
    args = map_options_to_agent_args(
        ClaudeAgentOptions(resume="sid", continue_conversation=True, fork_session=True, setting_sources=[])
    )
    assert "--resume" not in args
    assert "--continue" not in args
    assert "--fork-session" not in args
    assert not any(arg.startswith("--setting-sources") for arg in args)


def test_system_prompt_string_replaces() -> None:
    assert _system_prompt_args("be terse") == ["--system-prompt", "be terse"]


def test_system_prompt_preset_with_append() -> None:
    assert _system_prompt_args({"type": "preset", "preset": "claude_code", "append": "marker"}) == [
        "--append-system-prompt",
        "marker",
    ]


def test_system_prompt_preset_without_append_is_empty() -> None:
    assert _system_prompt_args({"type": "preset", "preset": "claude_code"}) == []


def test_system_prompt_none_is_empty() -> None:
    assert _system_prompt_args(None) == []


def test_resolve_cwd_defaults_to_process_cwd() -> None:
    assert resolve_cwd(ClaudeAgentOptions()) == Path.cwd().resolve()


def test_resolve_cwd_uses_given_cwd(tmp_path: Path) -> None:
    assert resolve_cwd(ClaudeAgentOptions(cwd=str(tmp_path))) == tmp_path.resolve()


def test_build_environment_overlays_options_env() -> None:
    options = ClaudeAgentOptions(env={"AGENT_SDK_PROBE": "value-1"})
    environment = _build_environment(options)
    by_key = {pair.key: pair.value for pair in environment.env_vars}
    assert by_key["AGENT_SDK_PROBE"] == "value-1"
    # The forwarded base env (os.environ) is still present alongside the overlay.
    assert "PATH" in by_key


def test_build_environment_overlay_overrides_forwarded_value() -> None:
    # PATH exists in os.environ; an explicit override must win and not be duplicated.
    options = ClaudeAgentOptions(env={"PATH": "/overridden"})
    environment = _build_environment(options)
    path_pairs = [pair for pair in environment.env_vars if pair.key == "PATH"]
    assert len(path_pairs) == 1
    assert path_pairs[0].value == "/overridden"


def test_build_agent_name_has_robinhood_prefix() -> None:
    name = _build_agent_name()
    assert str(name).startswith("robinhood-")
    assert len(str(name)) > len("robinhood-")


def test_options_with_overrides_copies_and_sets_only_given_fields() -> None:
    base = ClaudeAgentOptions(model="haiku", permission_mode="default")
    updated = _options_with_overrides(base, "sonnet", None)
    assert updated is not base
    assert updated.model == "sonnet"
    assert updated.permission_mode == "default"
    # The original is untouched.
    assert base.model == "haiku"


def test_options_with_overrides_returns_same_object_when_no_change() -> None:
    base = ClaudeAgentOptions(model="haiku")
    assert _options_with_overrides(base, None, None) is base


class _FakeBufferHost:
    """Minimal host stand-in whose read_text_file returns a settable stream buffer snapshot."""

    def __init__(self, content: str) -> None:
        self.content = content

    def read_text_file(self, _path: Path) -> str:
        return self.content


def _assistant_transcript_line(text: str, stop_reason: str | None) -> str:
    event = {
        "type": "assistant",
        "uuid": "a1",
        "sessionId": "sess-1",
        "message": {
            "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 3, "output_tokens": 7},
        },
    }
    return json.dumps(event)


def _make_ticker(
    local_host: Host,
    tmp_path: Path,
    transcript: str,
    buffer_content: str,
) -> tuple[_TurnDrainTicker, list[Message]]:
    """Build a _TurnDrainTicker over a host-backed transcript + stream buffer, capturing every sink call."""
    # The raw transcript is read at <events_path>/../logs/claude_transcript/events.jsonl;
    # mirror that on-disk layout so the host read resolves it (matching production).
    state_dir = tmp_path / "agent-x"
    events_path = state_dir / "events"
    events_path.mkdir(parents=True)
    transcript_path = state_dir / "logs" / "claude_transcript" / "events.jsonl"
    transcript_path.parent.mkdir(parents=True)
    # The transcript must end with a newline so the JSONL line is treated as a complete write
    # (split_complete_lines holds back any unterminated trailing partial).
    transcript_path.write_text(transcript + "\n")
    events_target = EventsTarget(host=local_host, events_path=events_path, display_name="test-agent")
    # model_construct bypasses validation so the lightweight in-memory transcript can stand in for a
    # fully built session; tick only reads events_target / seen_bytes / latest_* / options / agent.
    session = LiveSession.model_construct(
        options=ClaudeAgentOptions(model="claude-haiku-4-5"),
        cwd=Path("/work"),
        events_target=events_target,
        seen_bytes=0,
        latest_session_id=None,
        latest_model=None,
        agent=None,
        is_init_emitted=False,
    )
    synthesizer = StreamEventSynthesizer.model_construct(
        host=_FakeBufferHost(buffer_content), buffer_path=Path("/buffer"), reader=SnapshotDeltaReader()
    )
    captured: list[Message] = []
    ticker = _TurnDrainTicker(session=session, sink=captured.append, synthesizer=synthesizer)
    return ticker, captured


def _event_type(message: Any) -> str | None:
    return message.event["type"] if isinstance(message, StreamEvent) else None


def test_tick_emits_all_stream_events_including_close_framing_before_final_assistant_message(
    local_host: Host, tmp_path: Path
) -> None:
    transcript = _assistant_transcript_line("Hello world\nstreaming-tail", stop_reason="end_turn")
    ticker, captured = _make_ticker(local_host, tmp_path, transcript, buffer_content="id\nHello world\nstreaming-tail")

    assert ticker.tick() is True

    # The closing stream framing (message_stop) must land before the authoritative AssistantMessage.
    assistant_indices = [i for i, msg in enumerate(captured) if isinstance(msg, AssistantMessage)]
    message_stop_indices = [i for i, msg in enumerate(captured) if _event_type(msg) == "message_stop"]
    assert len(assistant_indices) == 1
    assert len(message_stop_indices) == 1
    assert message_stop_indices[0] < assistant_indices[0]
    # Every StreamEvent precedes the final transcript message, which is sinked last.
    assert isinstance(captured[-1], AssistantMessage)
    stream_event_indices = [i for i, msg in enumerate(captured) if isinstance(msg, StreamEvent)]
    assert all(i < assistant_indices[0] for i in stream_event_indices)


def test_tick_without_terminal_stop_leaves_partial_stream_unterminated(local_host: Host, tmp_path: Path) -> None:
    # Non-terminal stop: the agent has not cleanly completed, so no closing framing is emitted.
    transcript = _assistant_transcript_line("Hello world\nstreaming-tail", stop_reason=None)
    ticker, captured = _make_ticker(local_host, tmp_path, transcript, buffer_content="id\nHello world\nstreaming-tail")

    assert ticker.tick() is None

    assert not any(_event_type(msg) == "message_stop" for msg in captured)
    # Partial deltas are still surfaced; the sequence is simply left open.
    assert any(_event_type(msg) == "content_block_delta" for msg in captured)
