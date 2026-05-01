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


def test_deny_emits_deny_with_copy_pasteable_commands(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Golden path: the deny hook denies Task and gives Claude both `mngr create` and the wait command."""
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
    # Mode and intent stated up front so Claude knows why Task is denied.
    assert "deny mode" in reason
    assert "Task tool" in reason
    # Both phases of the workflow are present and copy-pasteable.
    assert "uv run mngr create" in reason
    assert "--type claude" in reason
    assert "--message-file" in reason
    assert "--label mngr_subagent_proxy=child" in reason
    assert "uv run python -m imbue.mngr_subagent_proxy.subagent_wait" in reason
    # Output protocol Claude should use to recognize the subagent's reply.
    assert "END_TURN:" in reason
    # Inspection commands so Claude (or the user) can observe progress.
    assert "mngr connect parent-agent--subagent-" in reason
    assert "mngr transcript parent-agent--subagent-" in reason


def test_deny_uses_target_name_with_parent_and_slug(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Target name embeds parent agent name, slugified description, and tool_use_id suffix.

    Same naming convention as the proxy spawn path so users can find
    deny-mode-spawned children with `mngr list --include
    'labels.mngr_subagent_proxy == "child"'`.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(
        _hook_input(tool_use_id="toolu_xyz98765432", description="Code Review!"),
        cwd_for_test=tmp_path,
        monkeypatch=clean_env,
    )

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "parent-agent--subagent-code-review-98765432" in reason


def test_deny_writes_prompt_sidefile_with_secure_perms(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The prompt is written to subagent_prompts/<tid>.md with 0600 perms.

    Claude is told to pass that file via ``mngr create --message-file``,
    which avoids embedding multi-line prompts inside a deny message and
    lets the prompt contain shell metacharacters safely.
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


def test_deny_does_not_create_proxy_machinery(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """No subagent_map/, no proxy_commands/, no wait scripts, no env files.

    Deny mode is the lighter path: the only sidefile is the prompt
    itself. This test guards against accidentally re-introducing
    proxy-mode plumbing.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    _run_deny(_hook_input(), cwd_for_test=tmp_path, monkeypatch=clean_env)

    assert not (state_dir / "subagent_map").exists()
    assert not (state_dir / "proxy_commands").exists()
    assert not (state_dir / "subagent_results").exists()


def test_deny_run_in_background_uses_background_phrasing(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """When run_in_background=True, the deny reason omits the synchronous wait step.

    A background Task call expects to return immediately with a poll
    handle. The deny reason should not instruct Claude to block on the
    subagent's end_turn -- only the spawn command and the inspection
    handles.
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
    assert "run_in_background=true" in reason
    assert "background" in reason.lower()
    # Background does NOT instruct Claude to wait synchronously.
    assert "subagent_wait" not in reason
    assert "END_TURN:" not in reason
    # But still gives the inspection handles.
    assert "mngr connect" in reason
    assert "mngr transcript" in reason


def test_deny_address_includes_parent_cwd(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """The mngr create address embeds the parent's cwd as <name>:<cwd>.

    This pins the subagent's worktree base to where the parent is
    actually running, matching the proxy's behavior. Without this, the
    user's shell cwd at the time they paste the command would
    accidentally re-base the subagent.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    cwd_for_test = tmp_path / "the-parent-cwd"
    cwd_for_test.mkdir()
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(_hook_input(), cwd_for_test=cwd_for_test, monkeypatch=clean_env)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert str(cwd_for_test) in reason


def test_deny_handles_missing_state_dir_with_inline_prompt(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """Without MNGR_AGENT_STATE_DIR, embed the prompt inline so Claude still has it.

    The plugin shouldn't normally see this -- the hook is only installed
    on mngr-managed agents. But defensive code matters because a
    pass-through here would silently allow the Task tool, defeating the
    point of deny mode.
    """
    # No env -- clean_env already cleared the relevant vars.
    payload = _hook_input(prompt="explore the codebase")
    response = _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    assert "deny mode" in reason
    # Prompt is inline since we couldn't write a sidefile.
    assert "explore the codebase" in reason


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
    # No prompt sidefile is created when there is nothing to write.
    assert not (state_dir / "subagent_prompts").exists()


def test_deny_handles_missing_tool_use_id_with_generic_deny(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A hook payload without tool_use_id emits a generic deny (can't synthesize unique target name)."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    payload: dict[str, object] = {"tool_input": {"prompt": "hi"}}
    response = _run_deny(payload, cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    assert not (state_dir / "subagent_prompts").exists()


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


def test_deny_long_prompt_written_to_file_not_inline(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A long prompt is written to the sidefile and the deny reason uses --message-file.

    Inlining a 50KB prompt into the deny reason would balloon the
    parent's transcript. The sidefile + ``--message-file`` indirection
    keeps the deny message short regardless of prompt size.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    # ~60KB of body text -- well past any reasonable inline budget.
    long_prompt = "Lorem ipsum " * 5000
    response = _run_deny(_hook_input(prompt=long_prompt), cwd_for_test=tmp_path, monkeypatch=clean_env)

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    # The full prompt isn't inline; only a small reference to the file is.
    assert long_prompt not in reason
    assert "--message-file" in reason
    # The sidefile contains the full prompt.
    prompt_file = state_dir / "subagent_prompts" / "toolu_abc12345678.md"
    assert prompt_file.read_text() == long_prompt


def test_deny_target_name_slug_falls_back_when_description_missing(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """An empty description still produces a usable, deterministic target name.

    Same fallback behavior as the proxy spawn hook: slugify("") yields
    "" so we substitute the literal "subagent" placeholder.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(
        _hook_input(description=""),
        cwd_for_test=tmp_path,
        monkeypatch=clean_env,
    )

    reason = response["hookSpecificOutput"]["permissionDecisionReason"]
    assert "parent-agent--subagent-subagent-12345678" in reason


def test_deny_handles_failed_prompt_write_with_inline_fallback(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """If the prompt sidefile can't be written, fall back to inlining the prompt.

    Disk full / permission denied / readonly mount on $MNGR_AGENT_STATE_DIR
    would otherwise drop the prompt entirely. Inlining keeps Claude
    informed -- worse UX than the file path, but better than silent loss.

    We trigger a real OSError by pre-creating ``subagent_prompts`` as a
    regular file -- ``mkdir(parents=True, exist_ok=True)`` then raises
    ``FileExistsError`` (an OSError subclass) when it discovers a
    non-directory at that path. This exercises the same code path a
    real disk-full / read-only mount would.
    """
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Pre-create subagent_prompts as a file, so the hook's mkdir fails.
    (state_dir / "subagent_prompts").write_text("not a directory")
    _set_hook_env(clean_env, state_dir)

    response = _run_deny(_hook_input(prompt="search the repo"), cwd_for_test=tmp_path, monkeypatch=clean_env)

    hook_out = response["hookSpecificOutput"]
    assert hook_out["permissionDecision"] == "deny"
    reason = hook_out["permissionDecisionReason"]
    # Prompt was embedded inline because the sidefile write failed.
    assert "search the repo" in reason


def test_build_deny_reason_quotes_special_characters_in_paths(tmp_path: Path) -> None:
    """Paths with spaces or quotes are shell-quoted via shlex.quote.

    A user whose project lives under e.g. ``/Users/joe doe/repo`` would
    otherwise get an unparseable copy-pasteable command. The reason is
    addressed to Claude, which should run it via Bash verbatim.
    """
    spaced_dir = tmp_path / "with spaces"
    spaced_dir.mkdir()
    prompt_file = spaced_dir / "toolu_x.md"
    prompt_file.write_text("hi")

    reason = deny_hook.build_deny_reason(
        target_name="parent--subagent-foo-12345678",
        prompt_file=prompt_file,
        parent_cwd=str(spaced_dir),
        run_in_background=False,
    )

    # shlex.quote wraps the path in single quotes since it has spaces.
    quoted_address = f"'parent--subagent-foo-12345678:{spaced_dir}'"
    assert quoted_address in reason
