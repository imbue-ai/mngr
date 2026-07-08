"""Unit tests for the deny-mode PreToolUse:Agent hook.

The deny hook reads only env vars (``MNGR_SUBAGENT_DEPTH``,
``MNGR_MAX_SUBAGENT_DEPTH``) and the stdin/stdout streams; it performs
no filesystem I/O. Most tests therefore just declare ``hook_env`` /
``clean_env`` in the signature to trigger fixture setup of the env
without referencing the returned MonkeyPatch in the body.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

from imbue.mngr_claude_subagent_proxy.hooks import deny as deny_hook


def _hook_input(
    *,
    tool_use_id: str = "toolu_abc12345678",
    prompt: str = "find the readmes",
    description: str = "explore repo",
    run_in_background: bool = False,
    subagent_type: str = "general-purpose",
) -> dict[str, object]:
    return {
        "tool_use_id": tool_use_id,
        "tool_input": {
            "prompt": prompt,
            "description": description,
            "subagent_type": subagent_type,
            "run_in_background": run_in_background,
        },
    }


def _run_deny(payload: dict[str, object] | None) -> Any:
    """Run the deny hook against ``payload`` (or empty stdin); return parsed stdout JSON.

    The hook reads only env vars and stdin/stdout, so environment setup
    happens in the calling test via ``hook_env`` / ``clean_env`` fixtures;
    this helper just stages the I/O buffers.
    """
    raw = "" if payload is None else json.dumps(payload)
    stdin_buffer = io.StringIO(raw)
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)
    out = stdout_buffer.getvalue()
    assert out, "deny hook emitted nothing on stdout"
    parsed = json.loads(out)
    assert isinstance(parsed, dict)
    return parsed


def test_deny_emits_short_skill_pointer_reason(hook_env: pytest.MonkeyPatch) -> None:
    """Golden path: deny reason is a one-liner pointing at the mngr-proxy skill.

    No wait-script path, no target name, no prompt content -- the
    skill is the single source of truth for the protocol. ``hook_env``
    seeds the realistic ``MNGR_AGENT_STATE_DIR`` / ``MNGR_AGENT_NAME``
    env even though the deny hook ignores them.
    """
    del hook_env
    response = _run_deny(_hook_input())

    hook_out = response["hookSpecificOutput"]
    assert hook_out["hookEventName"] == "PreToolUse"
    assert hook_out["permissionDecision"] == "deny"
    assert "updatedInput" not in hook_out
    reason = hook_out["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert "deny mode" in reason
    assert "mngr-proxy" in reason
    # The skill is the single source of truth -- no wait-script path,
    # no target name, no prompt body should leak into the reason.
    assert "wait-" not in reason
    assert "find the readmes" not in reason
    assert "toolu_abc12345678" not in reason
    # Brevity: one short paragraph, not a wall of protocol.
    assert len(reason) < 300, f"deny reason should stay short; got {len(reason)} chars: {reason!r}"


def test_deny_does_not_create_any_sidefiles(
    state_dir: Path,
    hook_env: pytest.MonkeyPatch,
) -> None:
    """Deny mode never writes anything to disk.

    The skill teaches Claude to write its own prompt file before
    running ``mngr create --message-file``; the plugin's deny hook
    has no business pre-staging any sidefiles.
    """
    del hook_env
    _run_deny(_hook_input())

    assert not (state_dir / "subagent_prompts").exists()
    assert not (state_dir / "proxy_commands").exists()
    assert not (state_dir / "subagent_map").exists()
    assert not (state_dir / "subagent_results").exists()


@pytest.mark.parametrize(
    "payload_a, payload_b",
    [
        pytest.param(
            _hook_input(run_in_background=False),
            _hook_input(run_in_background=True),
            id="run_in_background",
        ),
        pytest.param(
            _hook_input(prompt="A" * 5000, description="big task"),
            _hook_input(prompt="x", description=""),
            id="prompt_and_description",
        ),
    ],
)
def test_deny_reason_is_uniform_across_tool_inputs(
    payload_a: dict[str, object],
    payload_b: dict[str, object],
    hook_env: pytest.MonkeyPatch,
) -> None:
    """The deny reason is purely a function of plugin mode + depth, not tool_input content.

    Two payloads that differ only in tool_input fields (``run_in_background``,
    ``prompt``, ``description``) must produce identical deny reasons --
    Claude Code's Bash tool already accepts ``run_in_background=true``, so
    a Task call that wanted backgrounding can just bash the skill-protocol
    commands that way; a DENY-specific flag would be redundant. Likewise,
    long vs. minimal prompts must not trigger per-call customization.
    """
    del hook_env
    response_a = _run_deny(payload_a)
    response_b = _run_deny(payload_b)

    reason_a = response_a["hookSpecificOutput"]["permissionDecisionReason"]
    reason_b = response_b["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason_a == reason_b
    # Pin the absence of any PROXY-mode-style branching for the canonical
    # sync vs. background case: there is no DENY-specific spawn-only flag.
    assert "--spawn-only" not in reason_a


def test_deny_does_not_require_state_dir_or_parent_name(clean_env: pytest.MonkeyPatch) -> None:
    """Deny mode runs cleanly without MNGR_AGENT_STATE_DIR / MNGR_AGENT_NAME.

    The deny hook does no per-agent IO -- it just emits a constant
    skill-pointer reason -- so it should not depend on any mngr
    environment variables. Pin that contract by using ``clean_env``
    (which delenv()s the full set of subagent-proxy env vars) rather
    than the seeded ``hook_env``.
    """
    del clean_env
    response = _run_deny(_hook_input())

    assert response["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "mngr-proxy" in response["hookSpecificOutput"]["permissionDecisionReason"]


def test_deny_at_max_depth_emits_depth_limit_deny(hook_env: pytest.MonkeyPatch) -> None:
    """At/above ``MNGR_MAX_SUBAGENT_DEPTH``, deny mode emits a depth-limit reason.

    Without this guard, a chain of subagents that follow the skill's
    spawn protocol would grow unbounded. The README's "Depth limit"
    section advertises this guard plugin-wide, so it must hold in
    DENY mode too.
    """
    hook_env.setenv("MNGR_SUBAGENT_DEPTH", "3")
    hook_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input())

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "depth limit" in reason
    assert "3/3" in reason
    assert "Cannot spawn nested Task tools" in reason
    # Skill pointer is replaced with the depth-limit reason at the limit.
    assert "mngr-proxy" not in reason


def test_deny_below_max_depth_emits_skill_pointer_reason(hook_env: pytest.MonkeyPatch) -> None:
    """Below the depth limit, deny mode emits the normal skill-pointer reason."""
    hook_env.setenv("MNGR_SUBAGENT_DEPTH", "2")
    hook_env.setenv("MNGR_MAX_SUBAGENT_DEPTH", "3")

    response = _run_deny(_hook_input())

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mngr-proxy" in reason
    assert "depth limit" not in reason


def test_deny_typed_subagent_reason_points_at_resolved_agent_definition(
    hook_env: pytest.MonkeyPatch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``tool_input.subagent_type`` resolves to an on-disk agent
    definition, the deny reason names the resolved path so Claude knows
    to prepend its body to the prompt file before ``mngr create``.

    Without this, DENY mode silently strips the typed-subagent system
    prompt: Claude follows the skill's two-command protocol with only
    the parent's prompt, and the spawned subagent runs without the
    behavior the agent type was designed for (verify-and-fix's
    autofix instructions, code-reviewer's review checklist, etc.).
    """
    # ``hook_env`` is declared so the env baseline (state dir, parent name) is set up; the body
    # doesn't reference it directly.
    del hook_env
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    marketplace_agent = (
        fake_home
        / ".claude"
        / "plugins"
        / "marketplaces"
        / "imbue-code-guardian"
        / "plugins"
        / "imbue-code-guardian"
        / "agents"
        / "verify-and-fix.md"
    )
    marketplace_agent.parent.mkdir(parents=True)
    marketplace_agent.write_text("---\nname: verify-and-fix\ndescription: verify\n---\n\nSystem prompt body.\n")

    payload = _hook_input(subagent_type="imbue-code-guardian:verify-and-fix")
    response = _run_deny(payload)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    # Base skill pointer is still present.
    assert "mngr-proxy" in reason
    # Typed-subagent pointer appended with the resolved path.
    assert str(marketplace_agent) in reason
    assert "prepend the body" in reason
    # The body itself is NOT inlined -- the deny reason stays a pointer,
    # not the agent's full system prompt.
    assert "System prompt body." not in reason


def test_deny_typed_subagent_reason_omits_pointer_for_unresolved_type(
    hook_env: pytest.MonkeyPatch,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``tool_input.subagent_type`` is a built-in (``general-purpose``)
    or otherwise unresolvable, the deny reason is the unchanged short
    skill-pointer text -- no path appended.

    Pins the contract that the typed-subagent suffix is opt-in (only
    fires when there's something to point at), not an unconditional
    paragraph that crowds every deny.
    """
    del hook_env
    fake_home = tmp_path / "fake_home_empty"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))

    response = _run_deny(_hook_input())
    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "mngr-proxy" in reason
    assert "prepend the body" not in reason
    assert ".md" not in reason


@pytest.mark.parametrize(
    "raw_stdin",
    [
        pytest.param(json.dumps(_hook_input()), id="valid_input"),
        pytest.param("", id="empty_stdin"),
        pytest.param("{not json", id="malformed_json"),
        pytest.param("[1, 2, 3]", id="non_dict_json"),
        pytest.param("{}", id="empty_object"),
        pytest.param(json.dumps({"tool_input": {"prompt": "hi"}}), id="missing_tool_use_id"),
        pytest.param(json.dumps({"tool_use_id": "toolu_x"}), id="missing_prompt"),
    ],
)
def test_deny_is_uniform_across_stdin_shapes(raw_stdin: str, hook_env: pytest.MonkeyPatch) -> None:
    """The deny reason is uniform for any stdin shape -- no passthrough, skill pointer always present.

    Two invariants in one parametrized table:
    (1) ``permissionDecision == "deny"`` for every stdin shape -- a
        passthrough would let the native Task tool run, defeating the
        point of deny mode.
    (2) ``"mngr-proxy" in permissionDecisionReason`` -- the deny
        reason consistently points at the skill, regardless of what
        Claude was trying to delegate (or whether the hook input even
        parsed).

    Covers valid, empty, malformed, non-dict, well-formed-but-empty,
    and partially-populated stdin shapes.
    """
    del hook_env
    stdin_buffer = io.StringIO(raw_stdin)
    stdout_buffer = io.StringIO()
    deny_hook.run(stdin_buffer, stdout_buffer)
    hook_out = json.loads(stdout_buffer.getvalue())["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert "mngr-proxy" in hook_out["permissionDecisionReason"]
