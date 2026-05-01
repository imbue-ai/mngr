"""Unit tests for the deny-mode PreToolUse:Agent hook."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_subagent_proxy.hooks import deny as deny_hook


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
    payload: dict[str, object] | None,
    *,
    cwd_for_test: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    """Run the deny hook with a chdir to cwd_for_test; return parsed stdout JSON."""
    monkeypatch.chdir(cwd_for_test)
    raw = "" if payload is None else json.dumps(payload)
    stdin_buffer = io.StringIO(raw)
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)
    out = stdout_buffer.getvalue()
    assert out, "deny hook emitted nothing on stdout"
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    return parsed


def test_deny_emits_short_skill_pointer_reason(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """Golden path: deny reason is a one-liner pointing at the mngr-subagents skill.

    No wait-script path, no target name, no prompt content -- the
    skill is the single source of truth for the protocol.
    """
    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=hook_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_out
    reason = hook_out["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert "deny mode" in reason
    assert "mngr-subagents" in reason
    # The skill is the single source of truth -- no wait-script path,
    # no target name, no prompt body should leak into the reason.
    assert "wait-" not in reason
    assert "find the readmes" not in reason
    assert "toolu_abc12345678" not in reason
    # Brevity: one short paragraph, not a wall of protocol.
    assert len(reason) < 300, f"deny reason should stay short; got {len(reason)} chars: {reason!r}"


def test_deny_does_not_create_any_sidefiles(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """Deny mode never writes anything to disk.

    The skill teaches Claude to write its own prompt file before
    running ``mngr create --message-file``; the plugin's deny hook
    has no business pre-staging any sidefiles.
    """
    _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=hook_env)

    assert not (state_dir / "subagent_prompts").exists()
    assert not (state_dir / "proxy_commands").exists()
    assert not (state_dir / "subagent_map").exists()
    assert not (state_dir / "subagent_results").exists()


def test_deny_reason_does_not_branch_on_run_in_background(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """The deny reason is identical regardless of any tool_input field.

    Claude Code's Bash tool already accepts ``run_in_background=true``,
    so a Task call that wanted backgrounding can just bash the
    skill-protocol commands that way. A second DENY-specific flag
    would be a redundant way to say the same thing.
    """
    response_sync = _run_deny(
        _hook_input(run_in_background=False),
        cwd_for_test=tmp_path,
        monkeypatch=hook_env,
    )
    response_bg = _run_deny(
        _hook_input(run_in_background=True),
        cwd_for_test=tmp_path,
        monkeypatch=hook_env,
    )

    sync_reason = response_sync["hookSpecificOutput"]["permissionDecisionReason"]
    bg_reason = response_bg["hookSpecificOutput"]["permissionDecisionReason"]
    assert sync_reason == bg_reason
    assert "--spawn-only" not in sync_reason


def test_deny_reason_is_uniform_regardless_of_tool_input(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """Every Task-call shape gets the same deny reason; no per-call customization."""
    long_prompt_response = _run_deny(
        _hook_input(prompt="A" * 5000, description="big task"),
        cwd_for_test=tmp_path,
        monkeypatch=hook_env,
    )
    minimal_response = _run_deny(_hook_input(prompt="x", description=""), cwd_for_test=tmp_path, monkeypatch=hook_env)

    assert (
        long_prompt_response["hookSpecificOutput"]["permissionDecisionReason"]
        == minimal_response["hookSpecificOutput"]["permissionDecisionReason"]
    )


def test_deny_does_not_require_state_dir_or_parent_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deny mode runs cleanly without MNGR_AGENT_STATE_DIR / MNGR_AGENT_NAME.

    The deny hook does no per-agent IO -- it just emits a constant
    skill-pointer reason -- so it should not depend on any mngr
    environment variables. Pin that contract.
    """
    monkeypatch.delenv("MNGR_AGENT_STATE_DIR", raising=False)
    monkeypatch.delenv("MNGR_AGENT_NAME", raising=False)
    monkeypatch.delenv("MNGR_SUBAGENT_DEPTH", raising=False)
    monkeypatch.delenv("MNGR_MAX_SUBAGENT_DEPTH", raising=False)

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=monkeypatch)

    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mngr-subagents" in response["hookSpecificOutput"]["permissionDecisionReason"]


@pytest.mark.parametrize(
    ("case", "raw_stdin"),
    [
        ("empty", ""),
        ("malformed_json", "{not json"),
        ("non_object_json", "[1, 2, 3]"),
        ("empty_object", "{}"),
    ],
)
def test_deny_emits_skill_pointer_for_any_stdin(
    case: str,
    raw_stdin: str,
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """The deny reason is uniform for any stdin shape.

    The hook deliberately ignores stdin content -- the deny is the
    same regardless of what Claude was trying to delegate. Empty,
    malformed, non-dict JSON, and well-formed-but-empty all get the
    same skill-pointer reason.
    """
    # `case` is consumed by parametrize as the test id; not used in the body.
    del case
    hook_env.chdir(tmp_path)
    stdin_buffer = io.StringIO(raw_stdin)
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)

    response = json.loads(stdout_buffer.getvalue())
    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "mngr-subagents" in hook_out["permissionDecisionReason"]


def test_deny_at_max_depth_emits_depth_limit_deny(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """At/above ``MNGR_MAX_SUBAGENT_DEPTH``, deny mode emits a depth-limit reason.

    Without this guard, a chain of subagents that follow the skill's
    spawn protocol would grow unbounded. The README's "Depth limit"
    section advertises this guard plugin-wide, so it must hold in
    DENY mode too.
    """
    hook_env.setenv("MNGR_SUBAGENT_DEPTH", "3")
    hook_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=hook_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "depth limit" in reason
    assert "3/3" in reason
    assert "Cannot spawn nested Task tools" in reason
    # Skill pointer is replaced with the depth-limit reason at the limit.
    assert "mngr-subagents" not in reason


def test_deny_below_max_depth_emits_skill_pointer_reason(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """Below the depth limit, deny mode emits the normal skill-pointer reason."""
    hook_env.setenv("MNGR_SUBAGENT_DEPTH", "2")
    hook_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=hook_env)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mngr-subagents" in reason
    assert "depth limit" not in reason


def test_deny_never_allows_passthrough(
    tmp_path: Path,
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """No code path in the deny hook may emit permissionDecision=allow.

    A pass-through would let the native Task tool run, defeating the
    point of deny mode.
    """
    cases: list[tuple[str, str]] = [
        ("valid input", json.dumps(_hook_input())),
        ("empty stdin", ""),
        ("malformed JSON", "{not json"),
        ("non-dict JSON", "[1, 2, 3]"),
        ("payload missing tool_use_id", json.dumps({"tool_input": {"prompt": "hi"}})),
        ("payload missing prompt", json.dumps({"tool_use_id": "toolu_x"})),
    ]
    hook_env.chdir(tmp_path)
    for label, raw_stdin in cases:
        stdin_buffer = io.StringIO(raw_stdin)
        stdout_buffer = io.StringIO()
        deny_hook.run(stdin_buffer, stdout_buffer)
        decision = json.loads(stdout_buffer.getvalue())["hookSpecificOutput"]["permissionDecision"]
        assert decision == "deny", f"case {label!r} unexpectedly emitted {decision!r}"
