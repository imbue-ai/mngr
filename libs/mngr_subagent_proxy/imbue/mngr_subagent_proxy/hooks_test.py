"""Unit tests for the subagent-proxy python hook modules.

Each hook module exposes a ``run(stdin, stdout[, ...callables])`` core that
takes its I/O streams and side-effecting helpers as parameters. Tests pass
``StringIO`` buffers and stub callables directly, so subprocess-spawning
side effects (destroy / background reap) are intercepted without
monkey-patching module-level names.
"""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path

import pytest

from imbue.mngr_subagent_proxy.hooks import reap as reap_hook
from imbue.mngr_subagent_proxy.hooks import rewrite as rewrite_hook
from imbue.mngr_subagent_proxy.hooks import spawn as spawn_hook


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear subagent-proxy env vars so individual tests set only what they need."""
    for name in (
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_NAME",
        "MNGR_SUBAGENT_DEPTH",
        "MNGR_MAX_SUBAGENT_DEPTH",
        "MNGR_SUBAGENT_REAP_BACKGROUND",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _mode_bits(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _set_spawn_env(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MNGR_AGENT_NAME", "parent-agent")


def test_spawn_rewrites_input(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    """PreToolUse hook rewrites the Agent invocation to the mngr proxy."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_spawn_env(clean_env, state_dir)

    hook_input: dict[str, object] = {
        "tool_use_id": "toolu_abc12345678",
        "tool_input": {
            "prompt": "find all readmes",
            "description": "explore repo",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    }
    stdin_buffer = io.StringIO(json.dumps(hook_input))
    stdout_buffer = io.StringIO()
    spawn_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "allow"
    updated = hook_out["updatedInput"]
    assert updated["subagent_type"] == "mngr-proxy"
    assert updated["run_in_background"] is False
    # Prompt embeds the literal absolute wait-script path (no shell variables
    # for Haiku to interpret) and the target agent name.
    prompt_text = updated["prompt"]
    assert "parent-agent--subagent-" in prompt_text
    assert f"bash {state_dir}/proxy_commands/wait-toolu_abc12345678.sh" in prompt_text
    assert "DONE" in prompt_text

    tid = "toolu_abc12345678"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    script_file = state_dir / "proxy_commands" / f"wait-{tid}.sh"

    assert prompt_file.read_text() == "find all readmes"
    map_data = json.loads(map_file.read_text())
    assert set(map_data.keys()) == {"target_name", "subagent_type", "parent_cwd", "run_in_background"}
    assert map_data["subagent_type"] == "general-purpose"
    assert map_data["run_in_background"] is False
    assert map_data["target_name"].startswith("parent-agent--subagent-")
    assert script_file.is_file()

    script_contents = script_file.read_text()
    assert script_contents.startswith("#!/usr/bin/env bash")
    assert "uv run mngr create" in script_contents
    assert "uv run python -m imbue.mngr_subagent_proxy.subagent_wait" in script_contents

    assert _mode_bits(prompt_file) == 0o600
    assert _mode_bits(map_file) == 0o600
    assert _mode_bits(script_file) == 0o755


def test_spawn_depth_limit_denies_with_reason(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    """At max depth, the hook denies the Task tool with an explanatory reason."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_spawn_env(clean_env, state_dir)
    clean_env.setenv("MNGR_SUBAGENT_DEPTH", "3")
    clean_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    hook_input: dict[str, object] = {
        "tool_use_id": "toolu_depth1234567",
        "tool_input": {
            "prompt": "deep nested work",
            "description": "nested task",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    }
    stdin_buffer = io.StringIO(json.dumps(hook_input))
    stdout_buffer = io.StringIO()
    spawn_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_out
    reason = hook_out.get("permissionDecisionReason", "")
    assert "depth limit" in reason
    assert "3/3" in reason
    assert "Cannot spawn nested Task tools" in reason

    # No side-files should be created for a denied call.
    for subdir in ("subagent_prompts", "subagent_map", "proxy_commands"):
        entries = list((state_dir / subdir).iterdir()) if (state_dir / subdir).exists() else []
        assert entries == []


def test_spawn_passes_through_without_env(tmp_path: Path, clean_env: pytest.MonkeyPatch) -> None:
    """Missing state-dir env var causes an allow pass-through."""
    del tmp_path  # unused
    del clean_env  # the fixture's job is the env cleanup
    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": "tid", "tool_input": {"prompt": "hi"}}))
    stdout_buffer = io.StringIO()
    spawn_hook.run(stdin_buffer, stdout_buffer)
    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "allow"
    assert "updatedInput" not in hook_out


def test_rewrite_substitutes_output_and_cleans_up(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """PostToolUse hook swaps the tool output with the harvested result and cleans up."""
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    tid = "toolu_xyz"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    map_file.write_text(
        json.dumps(
            {
                "target_name": "foo-bar",
                "subagent_type": "general-purpose",
                "parent_cwd": "/tmp",
                "run_in_background": False,
            }
        )
    )
    expected_output = "This is the real subagent result.\nWith newlines."
    result_file.write_text(expected_output)
    prompt_file.write_text("original prompt")

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    stdout_buffer = io.StringIO()
    rewrite_hook.run(stdin_buffer, stdout_buffer, destroy_callable=fake_destroy)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    assert hook_out["updatedToolOutput"] == expected_output

    # Side files are cleaned up.
    assert not map_file.exists()
    assert not result_file.exists()
    assert not prompt_file.exists()

    # Destroy was requested exactly once with the target name.
    assert len(destroy_calls) == 1
    target, log_path = destroy_calls[0]
    assert target == "foo-bar"
    assert log_path == state_dir / "subagent_destroy.log"


def test_rewrite_missing_result_emits_error(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """When the result file is missing, the hook emits an ERROR sentinel."""
    state_dir = tmp_path / "state"
    (state_dir / "subagent_map").mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    tid = "toolu_err"
    target_name = "foo-err-target"
    (state_dir / "subagent_map" / f"{tid}.json").write_text(
        json.dumps(
            {
                "target_name": target_name,
                "subagent_type": "general-purpose",
                "parent_cwd": "/tmp",
                "run_in_background": False,
            }
        )
    )

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    stdout_buffer = io.StringIO()
    rewrite_hook.run(stdin_buffer, stdout_buffer, destroy_callable=lambda _name, _log: None)

    response = json.loads(stdout_buffer.getvalue())
    output = response["hookSpecificOutput"]["updatedToolOutput"]
    assert "ERROR" in output
    assert target_name in output


def test_rewrite_ignores_unmapped_tool_use_id(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """If no map file exists for the tool_use_id, the hook is a no-op."""
    state_dir = tmp_path / "state"
    (state_dir / "subagent_map").mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": "untracked_tid"}))
    stdout_buffer = io.StringIO()
    rewrite_hook.run(stdin_buffer, stdout_buffer, destroy_callable=fake_destroy)
    assert stdout_buffer.getvalue() == ""
    assert destroy_calls == []


def test_reap_fast_path_empty_state(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Reaper exits immediately when subagent_map/ is missing or empty, without dispatching."""
    spawn_calls: list[None] = []

    def fake_spawn() -> None:
        spawn_calls.append(None)

    # Case 1: state dir does not exist at all.
    state_dir = tmp_path / "no-state"
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))
    reap_hook.run(io.StringIO(""), spawn_background_callable=fake_spawn)
    assert spawn_calls == []

    # Case 2: state dir exists with an empty subagent_map/.
    state_dir2 = tmp_path / "empty-state"
    (state_dir2 / "subagent_map").mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir2))
    reap_hook.run(io.StringIO(""), spawn_background_callable=fake_spawn)
    assert spawn_calls == []


def test_reap_with_work_spawns_background_child(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """When map entries exist, the reaper dispatches a detached background child."""
    state_dir = tmp_path / "state"
    (state_dir / "subagent_map").mkdir(parents=True)
    (state_dir / "subagent_map" / "toolu_tid1234.json").write_text(
        json.dumps(
            {
                "target_name": "fake-agent",
                "subagent_type": "general-purpose",
                "parent_cwd": "/tmp",
                "run_in_background": False,
            }
        )
    )

    spawn_calls: list[None] = []

    def fake_spawn() -> None:
        spawn_calls.append(None)

    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    reap_hook.run(io.StringIO(""), spawn_background_callable=fake_spawn)

    assert len(spawn_calls) == 1


def test_reap_background_worker_cleans_up_missing_agent(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Background reaper drops side files for map entries whose target agent is gone."""
    clean_env.setenv("MNGR_SUBAGENT_REAP_BACKGROUND", "1")
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    tid = "toolu_missing1234"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    map_file.write_text(
        json.dumps(
            {
                "target_name": "vanished-agent",
                "subagent_type": "general-purpose",
                "parent_cwd": "/tmp",
                "run_in_background": False,
            }
        )
    )
    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    result_file.write_text("leftover")

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    reap_hook.run(
        io.StringIO(""),
        list_callable=lambda: {},
        destroy_callable=fake_destroy,
    )

    assert not map_file.exists()
    assert not result_file.exists()
    # Vanished agent: no destroy call is required.
    assert destroy_calls == []


def test_slugify_caps_length_and_collapses_runs() -> None:
    """slugify lowercases, collapses non-alphanumeric runs to single dashes, and caps at 30 chars."""
    assert spawn_hook.slugify("Hello, World!") == "hello-world"
    assert spawn_hook.slugify("----") == ""
    assert spawn_hook.slugify("a" * 50) == "a" * 30
    assert spawn_hook.slugify("A B  C") == "a-b-c"


def test_spawn_env_vars_from_real_os_env(clean_env: pytest.MonkeyPatch) -> None:
    """Sanity: helpers read from the actual os.environ (not a closure)."""
    # This ensures we don't accidentally snapshot at import time.
    assert spawn_hook._parse_int_env("__DOES_NOT_EXIST__", 42) == 42
    clean_env.setenv("__SPAWN_TEST_INT__", "7")
    assert spawn_hook._parse_int_env("__SPAWN_TEST_INT__", 0) == 7
