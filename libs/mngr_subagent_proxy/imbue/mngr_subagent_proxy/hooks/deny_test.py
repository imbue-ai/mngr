"""Unit tests for the deny-mode PreToolUse:Agent hook."""

from __future__ import annotations

import io
import json
import stat
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_subagent_proxy.hooks import deny as deny_hook


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Clear hook env vars so individual tests set only what they need."""
    for name in (
        "MNGR_AGENT_STATE_DIR",
        "MNGR_AGENT_NAME",
        "MNGR_SUBAGENT_DEPTH",
        "MNGR_MAX_SUBAGENT_DEPTH",
    ):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _set_hook_env(monkeypatch: pytest.MonkeyPatch, state_dir: Path) -> None:
    monkeypatch.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))
    monkeypatch.setenv("MNGR_AGENT_NAME", "parent-agent")


def _hook_input(
    *,
    tool_use_id: str = "toolu_abc12345678",
    prompt: str = "find the readmes",
    description: str = "explore repo",
    run_in_background: bool = False,
) -> dict[str, object]:
    return {
        "tool_use_id": tool_use_id,
        "tool_input": {
            "prompt": prompt,
            "description": description,
            "subagent_type": "general-purpose",
            "run_in_background": run_in_background,
        },
    }


def _run_deny(
    payload: dict[str, object],
    *,
    cwd_for_test: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Run the deny hook with a chdir to cwd_for_test; return parsed stdout JSON.

    Returns Any so callers can subscribe into nested fields without
    intermediate ``isinstance`` casts -- matches the pattern in
    hooks_test.py.
    """
    monkeypatch.chdir(cwd_for_test)
    stdin_buffer = io.StringIO(json.dumps(payload))
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)
    raw = stdout_buffer.getvalue()
    assert raw, "deny hook emitted nothing on stdout"
    parsed = json.loads(raw)
    assert isinstance(parsed, dict)
    return parsed


def test_deny_emits_short_deny_with_wait_script_path(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Golden path: the deny hook emits a short reason pointing at a per-Task wait-script.

    Detailed protocol (subagent_wait parsing, inspection commands, etc.)
    lives in the ``mngr-subagents`` Claude skill, not in the deny
    reason. The reason itself is a one-liner so it does not crowd the
    parent's transcript on every Task call.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_out
    reason = hook_out["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert "Use a mngr subagent" in reason
    assert "mngr-subagents" in reason
    expected_script = state_dir / "proxy_commands" / "wait-toolu_abc12345678.sh"
    assert f"bash {expected_script}" in reason
    # Brevity: the verbose protocol lives in the skill, not here.
    assert len(reason) < 500, f"deny reason should stay short; got {len(reason)} chars: {reason!r}"


def test_deny_writes_wait_script_with_executable_perms(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Per-Task wait-script lands at proxy_commands/wait-<tid>.sh with 0755 perms.

    The script is what Claude executes in Bash. It must be readable +
    executable by the user; permissions match the proxy-mode wait-script.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    script = state_dir / "proxy_commands" / "wait-toolu_abc12345678.sh"
    assert script.is_file()
    assert stat.S_IMODE(script.stat().st_mode) == 0o755
    body = script.read_text()
    assert body.startswith("#!/usr/bin/env bash")
    assert "uv run mngr create" in body
    assert "--type claude" in body
    assert "--label mngr_subagent_proxy=child" in body
    # --reuse keeps creation idempotent on partial-create failures.
    assert "--reuse" in body
    assert "uv run python -m imbue.mngr_subagent_proxy.subagent_wait" in body
    # Spawn-only branch for run_in_background; same flag the script
    # accepts when run by the deny hook in background mode.
    assert '"${1:-}" = "--spawn-only"' in body


def test_deny_wait_script_traps_env_file_cleanup() -> None:
    """The wait script installs an EXIT trap before the env-file capture.

    Same pattern as the proxy-mode wait-script: a signal arriving
    between the env redirect and the trap cannot leave parent secrets
    on disk, because the trap is in place before the redirect runs.
    """
    body = deny_hook.build_deny_wait_script(
        tool_use_id="toolu_test_trap",
        target_name="parent--subagent-foo-trap",
        parent_cwd="/tmp/somewhere",
    )
    init_idx = body.find('if [ ! -f "$INIT_FLAG" ]; then')
    create_idx = body.find("mngr create")
    env_capture_idx = body.find('> "$ENV_FILE"')
    trap_install_idx = body.find("trap 'shred -u")
    trap_clear_idx = body.find("trap - EXIT")
    assert init_idx >= 0 and create_idx >= 0 and env_capture_idx >= 0
    assert trap_install_idx >= 0 and trap_clear_idx >= 0
    assert init_idx < trap_install_idx < env_capture_idx < create_idx < trap_clear_idx, (
        "EXIT trap must be installed BEFORE env-capture redirect (and before mngr create) "
        "and cleared AFTER successful shred"
    )


def test_deny_uses_target_name_with_parent_and_slug(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The wait-script bakes in the target name <parent>--subagent-<slug>-<tid_suffix>.

    Same naming convention as the proxy spawn path so users can find
    deny-mode-spawned children with `mngr list --include
    'labels.mngr_subagent_proxy == "child"'`.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(
        _hook_input(tool_use_id="toolu_xyz98765432", description="Code Review!"),
        cwd_for_test=tmp_path,
        monkeypatch=clean_env,
    )

    script_body = (state_dir / "proxy_commands" / "wait-toolu_xyz98765432.sh").read_text()
    assert "parent-agent--subagent-code-review-98765432" in script_body


def test_deny_writes_prompt_sidefile_with_secure_perms(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The prompt is written to subagent_prompts/<tid>.md with 0600 perms.

    The wait-script passes the prompt file via ``mngr create
    --message-file``, which avoids embedding multi-line prompts in
    shell args and lets the prompt contain shell metacharacters safely.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    payload = _hook_input(prompt="Look for `ls -la`; report 'count=5'.\nMulti-line ok.")
    _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    prompt_file = state_dir / "subagent_prompts" / "toolu_abc12345678.md"
    assert prompt_file.is_file()
    assert prompt_file.read_text() == "Look for `ls -la`; report 'count=5'.\nMulti-line ok."
    assert stat.S_IMODE(prompt_file.stat().st_mode) == 0o600


def test_deny_does_not_create_proxy_only_machinery(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Deny mode does NOT create subagent_map/, subagent_results/, or watermark sidefiles.

    Those exist only in PROXY mode where the cleanup hook + Haiku
    permission redo machinery need them. Deny mode uses just the prompt
    sidefile and the wait-script.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    assert not (state_dir / "subagent_map").exists()
    assert not (state_dir / "subagent_results").exists()
    # proxy_commands/ DOES exist (wait-script lives there) but should
    # not contain map files or watermark files.
    proxy_cmds = state_dir / "proxy_commands"
    files = sorted(p.name for p in proxy_cmds.iterdir())
    assert files == ["wait-toolu_abc12345678.sh"]


def test_deny_run_in_background_uses_spawn_only_flag(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """When run_in_background=True the deny reason instructs Claude to pass --spawn-only.

    Same wait-script handles both modes; the flag tells it to skip the
    blocking subagent_wait step.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(
        _hook_input(run_in_background=True),
        cwd_for_test=tmp_path,
        monkeypatch=clean_env,
    )

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "--spawn-only" in reason
    assert "background" in reason.lower()
    assert "mngr-subagents" in reason


def test_deny_wait_script_baked_with_parent_cwd(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The wait-script bakes the parent's cwd in as PARENT_CWD.

    This pins the subagent's worktree base to where the parent is
    actually running; Claude pasting the command from another cwd does
    not accidentally re-base the subagent.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cwd_for_test = tmp_path / "the-parent-cwd"
    cwd_for_test.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(_hook_input(), cwd_for_test=cwd_for_test, monkeypatch=clean_env)

    script_body = (state_dir / "proxy_commands" / "wait-toolu_abc12345678.sh").read_text()
    assert f"PARENT_CWD={cwd_for_test}" in script_body


def test_deny_handles_missing_state_dir_with_generic_reason(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Without MNGR_AGENT_STATE_DIR, emit a generic deny pointing at the skill.

    The hook can't write a wait-script without a state dir -- but it
    still must DENY (never pass through), so Claude is informed that
    Task is disabled. The skill content tells Claude how to proceed.
    """
    payload = _hook_input(prompt="explore the codebase")
    response = _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "deny mode" in reason
    assert "mngr-subagents" in reason


def test_deny_handles_empty_stdin_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Empty stdin still emits a deny (never an allow pass-through)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    clean_env.chdir(tmp_path)
    stdin_buffer = io.StringIO("")
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "deny mode" in hook_out["permissionDecisionReason"]


def test_deny_handles_malformed_json_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Malformed stdin JSON falls back to a generic deny."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    clean_env.chdir(tmp_path)
    stdin_buffer = io.StringIO("{not json")
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"


def test_deny_handles_non_object_json_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """JSON that decodes to a non-dict (e.g. a list) falls back to generic deny."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    clean_env.chdir(tmp_path)
    stdin_buffer = io.StringIO("[1, 2, 3]")
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"


def test_deny_handles_missing_prompt_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A hook payload without a prompt emits a generic deny (no prompt to write)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    payload: dict[str, object] = {
        "tool_use_id": "toolu_abc12345678",
        "tool_input": {"description": "no prompt here"},
    }
    response = _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    # No prompt sidefile or wait-script created when there's nothing to write.
    assert not (state_dir / "subagent_prompts").exists()
    assert not (state_dir / "proxy_commands").exists()


def test_deny_handles_missing_tool_use_id_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A hook payload without tool_use_id emits a generic deny."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    payload: dict[str, object] = {"tool_input": {"prompt": "hi"}}
    response = _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert not (state_dir / "subagent_prompts").exists()
    assert not (state_dir / "proxy_commands").exists()


def test_deny_never_allows_passthrough(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """No code path in the deny hook may emit permissionDecision=allow.

    A pass-through would let the native Task tool run, defeating the
    point of deny mode. Test by exercising every failure mode and
    confirming the decision is always "deny".
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()

    cases: list[tuple[str, dict[str, object] | None, dict[str, str]]] = [
        ("no env, valid input", _hook_input(), {}),
        (
            "valid env, missing prompt",
            {"tool_use_id": "toolu_abc12345678", "tool_input": {"description": "x"}},
            {"MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_AGENT_NAME": "parent"},
        ),
        (
            "valid env, missing tool_use_id",
            {"tool_input": {"prompt": "x"}},
            {"MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_AGENT_NAME": "parent"},
        ),
        (
            "valid env, empty payload object",
            {},
            {"MNGR_AGENT_STATE_DIR": str(state_dir), "MNGR_AGENT_NAME": "parent"},
        ),
    ]
    clean_env.chdir(tmp_path)

    for label, payload, env_overrides in cases:
        for env_name, env_value in env_overrides.items():
            clean_env.setenv(env_name, env_value)
        stdin_buffer = io.StringIO("" if payload is None else json.dumps(payload))
        stdout_buffer = io.StringIO()
        deny_hook.run(stdin_buffer, stdout_buffer)
        decision = json.loads(stdout_buffer.getvalue())["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny", f"case {label!r} unexpectedly emitted {decision!r}"


def test_deny_long_prompt_written_to_file_only(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A long prompt is written to the sidefile, NEVER inline in the deny reason.

    Inlining a 50KB prompt into the deny reason would balloon the
    parent's transcript. The new short-reason design makes this even
    more important: the deny reason should always be a one-liner.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    # ~60KB of body text -- well past any reasonable inline budget.
    long_prompt = "Lorem ipsum " * 5000
    response = _run_deny(_hook_input(prompt=long_prompt), cwd_for_test=tmp_path, monkeypatch=clean_env)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    # Reason stays short; no inline prompt content.
    assert long_prompt not in reason
    assert len(reason) < 500
    # Sidefile contains the full prompt; wait-script consumes it via
    # ``mngr create --message-file``.
    prompt_file = state_dir / "subagent_prompts" / "toolu_abc12345678.md"
    assert prompt_file.read_text() == long_prompt


def test_deny_target_name_slug_falls_back_when_description_missing(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """An empty description still produces a usable, deterministic target name."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(
        _hook_input(description=""),
        cwd_for_test=tmp_path,
        monkeypatch=clean_env,
    )

    script_body = (state_dir / "proxy_commands" / "wait-toolu_abc12345678.sh").read_text()
    assert "parent-agent--subagent-subagent-12345678" in script_body


def test_deny_handles_failed_prompt_write_with_generic_reason(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """If the prompt sidefile can't be written, fall back to a generic deny.

    Disk full / permission denied / readonly mount on $MNGR_AGENT_STATE_DIR
    would otherwise leave the wait-script with no prompt to feed
    ``mngr create --message-file``. Generic deny + skill reference
    keeps Claude informed without inlining the prompt.

    We trigger a real OSError by pre-creating ``subagent_prompts`` as a
    regular file -- ``mkdir(parents=True, exist_ok=True)`` then raises
    ``FileExistsError`` (an OSError subclass).
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / "subagent_prompts").write_text("not a directory")
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(_hook_input(prompt="search the repo"), cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "deny mode" in reason
    assert "mngr-subagents" in reason


def test_deny_handles_failed_wait_script_write_with_generic_reason(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """If the wait-script can't be written, fall back to a generic deny.

    Same defensive behavior as the prompt-write failure path. Without
    the wait-script the deny reason has nowhere concrete to point, so
    we route Claude to the skill instead.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Pre-create proxy_commands as a file -- mkdir(parents=True) then raises.
    (state_dir / "proxy_commands").write_text("not a directory")
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "deny mode" in reason
    assert "mngr-subagents" in reason


def test_deny_at_max_depth_emits_depth_limit_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """At/above ``MNGR_MAX_SUBAGENT_DEPTH``, deny mode emits a depth-limit reason.

    Without this, a depth-N child would still be told to spawn another
    mngr subagent via the wait-script -- nothing would stop the chain
    from growing unbounded except whichever resource exhausts first.
    The README's "Depth limit" section advertises this guard plugin-wide,
    so it must hold in DENY mode too.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)
    clean_env.setenv("MNGR_SUBAGENT_DEPTH", "3")
    clean_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "depth limit" in reason
    assert "3/3" in reason
    assert "Cannot spawn nested Task tools" in reason
    # No sidefiles created when the depth-limit branch fires before any IO.
    assert not (state_dir / "subagent_prompts").exists()
    assert not (state_dir / "proxy_commands").exists()


def test_deny_below_max_depth_emits_normal_reason(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Below the depth limit, deny mode emits its normal short reason.

    Pin the boundary: depth 2/3 still produces the "Use a mngr subagent"
    reason and writes the wait-script. Only depth >= max_depth flips to
    the depth-limit reason.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)
    clean_env.setenv("MNGR_SUBAGENT_DEPTH", "2")
    clean_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "Use a mngr subagent" in reason
    assert "depth limit" not in reason
    assert (state_dir / "proxy_commands" / "wait-toolu_abc12345678.sh").is_file()


def test_build_deny_reason_quotes_special_characters_in_paths() -> None:
    """The wait-script path in the deny reason is shell-quoted via shlex.quote.

    A user whose project lives under e.g. ``/Users/joe doe/repo`` would
    otherwise get an unparseable copy-pasteable command.
    """
    spaced_script = Path("/with spaces/wait-x.sh")

    reason = deny_hook.build_deny_reason(spaced_script, run_in_background=False)

    # shlex.quote wraps the path in single quotes since it has spaces.
    assert "'/with spaces/wait-x.sh'" in reason
