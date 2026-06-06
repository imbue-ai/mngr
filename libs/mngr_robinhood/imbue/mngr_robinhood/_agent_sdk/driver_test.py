from pathlib import Path

from claude_agent_sdk import ClaudeAgentOptions

from imbue.mngr_robinhood._agent_sdk.driver import _build_agent_name
from imbue.mngr_robinhood._agent_sdk.driver import _build_environment
from imbue.mngr_robinhood._agent_sdk.driver import _system_prompt_args
from imbue.mngr_robinhood._agent_sdk.driver import map_options_to_agent_args
from imbue.mngr_robinhood._agent_sdk.driver import resolve_cwd


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
