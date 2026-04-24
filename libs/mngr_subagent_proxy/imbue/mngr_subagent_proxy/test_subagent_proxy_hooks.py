"""Release tests for the subagent-proxy python hook modules.

These tests invoke the hook modules as subprocesses (the same way Claude
Code invokes them), feeding hook-input JSON on stdin and asserting on the
hook's JSON response and the state-dir side files. Real ``uv`` calls are
avoided by mocking the binary on PATH.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest


def _make_noop_uv_on_path(tmp_path: Path) -> str:
    """Write a fake ``uv`` executable that exits 0 and prepend its dir to PATH."""
    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    uv_script = bin_dir / "uv"
    uv_script.write_text("#!/usr/bin/env bash\nexit 0\n")
    uv_script.chmod(0o755)
    return f"{bin_dir}{os.pathsep}{os.environ['PATH']}"


def _run_hook(
    module: str,
    hook_input: dict[str, object] | None,
    state_dir: Path,
    extra_env: dict[str, str] | None = None,
    path_override: str | None = None,
    stdin_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a hook module with the given JSON stdin and env."""
    env = {
        "MNGR_AGENT_STATE_DIR": str(state_dir),
        "MNGR_AGENT_NAME": "parent-agent",
        "MAIN_CLAUDE_SESSION_ID": "fake-session-id",
        "PATH": path_override if path_override is not None else os.environ["PATH"],
        "HOME": os.environ["HOME"],
    }
    if extra_env:
        env.update(extra_env)
    if stdin_text is not None:
        payload = stdin_text
    elif hook_input is not None:
        payload = json.dumps(hook_input)
    else:
        payload = ""
    return subprocess.run(
        [sys.executable, "-m", module],
        input=payload,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def _mode_bits(path: Path) -> int:
    """Return the permission bits of a file (e.g. 0o600)."""
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.release
def test_spawn_proxy_subagent_hook_rewrites_input(tmp_path: Path) -> None:
    """PreToolUse hook rewrites the Agent invocation to the mngr proxy."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    hook_input: dict[str, object] = {
        "tool_use_id": "toolu_abc12345678",
        "tool_input": {
            "prompt": "find all readmes",
            "description": "explore repo",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    }

    result = _run_hook("imbue.mngr_subagent_proxy.hooks.spawn", hook_input, state_dir)

    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    response = json.loads(result.stdout)
    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "allow"
    updated = hook_out["updatedInput"]
    assert updated["subagent_type"] == "mngr-proxy"
    assert updated["run_in_background"] is False
    prompt = updated["prompt"]
    assert prompt.startswith("MNGR_PROXY_AGENT=parent-agent--subagent-")
    assert "MNGR_PROXY_SCRIPT=" in prompt

    tid = "toolu_abc12345678"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    script_file = state_dir / "proxy_commands" / f"wait-{tid}.sh"

    assert prompt_file.is_file()
    assert prompt_file.read_text() == "find all readmes"

    map_data = json.loads(map_file.read_text())
    assert set(map_data.keys()) == {"target_name", "subagent_type", "parent_cwd", "run_in_background"}
    assert map_data["subagent_type"] == "general-purpose"
    assert map_data["run_in_background"] is False
    assert map_data["target_name"].startswith("parent-agent--subagent-")

    assert script_file.is_file()
    assert os.access(script_file, os.X_OK)
    script_contents = script_file.read_text()
    assert script_contents.startswith("#!/usr/bin/env bash")
    assert "uv run mngr create" in script_contents
    assert "uv run python -m imbue.mngr_subagent_proxy.subagent_wait" in script_contents

    assert _mode_bits(prompt_file) == 0o600
    assert _mode_bits(map_file) == 0o600
    assert _mode_bits(script_file) == 0o755


@pytest.mark.release
def test_spawn_proxy_hook_depth_limit_passes_through(tmp_path: Path) -> None:
    """At max depth, the hook allows the call through without rewriting."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    hook_input: dict[str, object] = {
        "tool_use_id": "toolu_depth1234567",
        "tool_input": {
            "prompt": "deep nested work",
            "description": "nested task",
            "subagent_type": "general-purpose",
            "run_in_background": False,
        },
    }

    result = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.spawn",
        hook_input,
        state_dir,
        extra_env={"MNGR_SUBAGENT_DEPTH": "3", "MNGR_MAX_SUBAGENT_DEPTH": "3"},
    )

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    response = json.loads(result.stdout)
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "allow"
    assert "updatedInput" not in hook_out
    system_message = hook_out.get("systemMessage", "")
    assert "depth limit" in system_message
    assert "3/3" in system_message

    assert not (state_dir / "subagent_prompts").exists() or not any((state_dir / "subagent_prompts").iterdir())
    assert not (state_dir / "subagent_map").exists() or not any((state_dir / "subagent_map").iterdir())
    assert not (state_dir / "proxy_commands").exists() or not any((state_dir / "proxy_commands").iterdir())


@pytest.mark.release
def test_rewrite_subagent_result_hook_substitutes_output(tmp_path: Path) -> None:
    """PostToolUse hook swaps the tool output with the harvested result."""
    state_dir = tmp_path / "state"
    (state_dir / "subagent_map").mkdir(parents=True)
    (state_dir / "subagent_results").mkdir(parents=True)
    (state_dir / "subagent_prompts").mkdir(parents=True)
    (state_dir / "proxy_commands").mkdir(parents=True)

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

    path_override = _make_noop_uv_on_path(tmp_path)
    hook_input: dict[str, object] = {
        "tool_use_id": tid,
        "tool_response": "ignored haiku output",
    }

    result = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.rewrite",
        hook_input,
        state_dir,
        path_override=path_override,
    )

    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    response = json.loads(result.stdout)
    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PostToolUse"
    assert hook_out["updatedToolOutput"] == expected_output

    # The hook launches `uv run mngr destroy` via a detached child. Give it
    # a moment to settle.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        if not map_file.exists() and not result_file.exists() and not prompt_file.exists():
            break
        time.sleep(0.05)

    assert not map_file.exists()
    assert not result_file.exists()
    assert not prompt_file.exists()


@pytest.mark.release
def test_reaper_fast_path_empty_state(tmp_path: Path) -> None:
    """Reaper exits 0 immediately when subagent_map/ is missing or empty, without invoking uv."""
    invocations_file = tmp_path / "uv_invocations.txt"

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    uv_script = bin_dir / "uv"
    uv_script.write_text(f'#!/usr/bin/env bash\necho "$@" >> {invocations_file}\nexit 0\n')
    uv_script.chmod(0o755)
    path_override = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    # Case 1: no state dir at all.
    state_dir = tmp_path / "no-state"
    result = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.reap",
        None,
        state_dir,
        path_override=path_override,
        stdin_text="",
    )
    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert not invocations_file.exists() or invocations_file.read_text().strip() == ""

    # Case 2: state dir with empty subagent_map/.
    state_dir2 = tmp_path / "empty-state"
    (state_dir2 / "subagent_map").mkdir(parents=True)
    result2 = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.reap",
        None,
        state_dir2,
        path_override=path_override,
        stdin_text="",
    )
    assert result2.returncode == 0, f"stderr={result2.stderr!r}"
    assert not invocations_file.exists() or invocations_file.read_text().strip() == ""


@pytest.mark.release
def test_reaper_returns_promptly_with_work(tmp_path: Path) -> None:
    """Reaper with a dummy map entry exits quickly because the heavy work is backgrounded."""
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

    bin_dir = tmp_path / "fake_bin"
    bin_dir.mkdir()
    uv_script = bin_dir / "uv"
    uv_script.write_text("#!/usr/bin/env bash\nsleep 60\n")
    uv_script.chmod(0o755)
    path_override = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"

    start = time.monotonic()
    result = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.reap",
        None,
        state_dir,
        path_override=path_override,
        stdin_text="",
    )
    elapsed = time.monotonic() - start

    assert result.returncode == 0, f"stderr={result.stderr!r}"
    assert elapsed < 5.0, f"reaper took {elapsed:.2f}s; expected quick return with backgrounded work"


@pytest.mark.release
def test_rewrite_hook_missing_result_emits_error(tmp_path: Path) -> None:
    """When the result file is missing, the hook emits an ERROR sentinel."""
    state_dir = tmp_path / "state"
    (state_dir / "subagent_map").mkdir(parents=True)

    tid = "toolu_err"
    target_name = "foo-err-target"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    map_file.write_text(
        json.dumps(
            {
                "target_name": target_name,
                "subagent_type": "general-purpose",
                "parent_cwd": "/tmp",
                "run_in_background": False,
            }
        )
    )

    path_override = _make_noop_uv_on_path(tmp_path)
    hook_input: dict[str, object] = {"tool_use_id": tid, "tool_response": "ignored"}

    result = _run_hook(
        "imbue.mngr_subagent_proxy.hooks.rewrite",
        hook_input,
        state_dir,
        path_override=path_override,
    )

    assert result.returncode == 0, f"stderr={result.stderr!r} stdout={result.stdout!r}"
    response = json.loads(result.stdout)
    output = response["hookSpecificOutput"]["updatedToolOutput"]
    assert "ERROR" in output
    assert target_name in output
