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

from imbue.mngr.interfaces.data_types import AgentDetails
from imbue.mngr.primitives import AgentLifecycleState
from imbue.mngr.utils.testing import make_test_agent_details
from imbue.mngr_subagent_proxy.hooks import cleanup as cleanup_hook
from imbue.mngr_subagent_proxy.hooks import reap as reap_hook
from imbue.mngr_subagent_proxy.hooks import spawn as spawn_hook


def _fake_list_with_state(target_name: str, state: AgentLifecycleState) -> dict[str, AgentDetails]:
    """Build a single-entry list-result with a minimum-fields AgentDetails."""
    return {target_name: make_test_agent_details(name=target_name, state=state)}


def _list_returns(agents: dict[str, AgentDetails] | None):
    """Build a list_callable stub that returns a fixed value."""

    def _stub() -> dict[str, AgentDetails] | None:
        return agents

    return _stub


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
    assert "MNGR_PROXY_END_OF_OUTPUT" in prompt_text
    # Bash-timeout retry guidance: a long-running subagent will outlast
    # Bash's 30-min timeout, and Haiku must re-run rather than hallucinate
    # "monitor" actions or fabricate text. Found live: a 2-hour
    # verify-and-fix run produced ~88 confused tool calls when the proxy
    # had no instruction for the timeout case.
    assert "times out" in prompt_text
    # Watermark plumbing must NOT leak into Haiku's prompt; Haiku has
    # historically been unreliable at parsing/passing numeric state.
    # The wait-script and python module own deduplication entirely.
    assert "AT_BYTES" not in prompt_text
    assert "--require-transcript-advance-past" not in prompt_text
    assert "--watermark-file" not in prompt_text
    # The 5-attempt cap must be SCOPED to the timeout-retry branch -- a
    # prior bug caused Haiku to apply the cap to NEED_PERMISSION too
    # and bail after 5 permission cycles.
    assert "ONLY to this timeout branch" in prompt_text

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
    assert "--type mngr-proxy-child" in script_contents
    assert "--label mngr_subagent_proxy=child" in script_contents
    # --reuse so partial-create failures are recoverable on retry.
    assert "--reuse" in script_contents
    assert "uv run python -m imbue.mngr_subagent_proxy.subagent_wait" in script_contents

    assert _mode_bits(prompt_file) == 0o600
    assert _mode_bits(map_file) == 0o600
    assert _mode_bits(script_file) == 0o755


def test_wait_script_idempotent_prelude_short_circuits_on_post_cleanup() -> None:
    """When prompt or map file is missing (PostToolUse already ran), the
    wait-script must emit MNGR_PROXY_END_OF_OUTPUT and exit 0 BEFORE
    attempting `mngr create`. Otherwise re-invocations by Haiku after
    PostToolUse cleanup would fail mngr create with "--message-file
    Path ... does not exist."

    Found live: a verify-and-fix subagent (running through the proxy)
    completed end_turn, PostToolUse cleaned up, Haiku re-ran the
    wait-script, and our previous version blew up on the missing
    prompt file.
    """
    script = spawn_hook.build_wait_script(
        tool_use_id="toolu_test_idempotent",
        target_name="parent--subagent-foo-test",
        parent_cwd="/tmp/somewhere",
    )
    # The idempotent guard appears BEFORE the mngr-create block.
    guard_idx = script.find('if [ ! -f "$PROMPT_FILE" ] || [ ! -f "$MAP_FILE" ]; then')
    create_idx = script.find("uv run mngr create")
    assert guard_idx >= 0, "wait-script missing idempotent prelude"
    assert create_idx >= 0, "wait-script missing mngr create call"
    assert guard_idx < create_idx, (
        "idempotent prelude must run BEFORE mngr create -- otherwise a "
        "re-invocation after PostToolUse cleanup would fail on missing prompt-file."
    )
    # The guard emits the sentinel and exits 0.
    guard_block = script[guard_idx:create_idx]
    assert "MNGR_PROXY_END_OF_OUTPUT" in guard_block
    assert "exit 0" in guard_block


def test_wait_script_owns_watermark_file_path() -> None:
    """The wait-script defines a per-tool_use_id watermark sidefile and
    passes its path to subagent_wait. Haiku never sees this path or the
    integer it contains -- the python module owns the dedup state.
    """
    script = spawn_hook.build_wait_script(
        tool_use_id="toolu_test_watermark",
        target_name="parent--subagent-foo-test",
        parent_cwd="/tmp/somewhere",
    )
    # The watermark file path is derived from TID, mirroring the other
    # per-tool_use_id sidefiles.
    assert 'WATERMARK_FILE="$STATE_DIR/proxy_commands/watermark-$TID"' in script
    # The wait module is invoked with the watermark file flag.
    assert "--watermark-file" in script
    assert '"$WATERMARK_FILE"' in script


def test_wait_script_traps_env_file_cleanup_on_failure() -> None:
    """If `mngr create` fails (network error, host-provisioning bug, etc.),
    the env-file containing parent secrets must still be removed. With
    `set -euo pipefail` and no trap, a failed mngr-create exits the script
    before the explicit shred, leaving secrets on disk.

    Found live: parent's $MNGR_AGENT_STATE_DIR/proxy_commands/ accumulated
    a stale env-<tid>.env from a run whose mngr-create had errored mid-flight.
    """
    script = spawn_hook.build_wait_script(
        tool_use_id="toolu_test_trap",
        target_name="parent--subagent-foo-trap",
        parent_cwd="/tmp/somewhere",
    )
    # The init branch installs an EXIT trap before the env-capture redirect
    # (so a signal between the redirect and the trap cannot leave secrets on
    # disk) and clears it after a successful shred.
    init_idx = script.find('if [ ! -f "$INIT_FLAG" ]; then')
    create_idx = script.find("mngr create")
    env_capture_idx = script.find('> "$ENV_FILE"')
    trap_install_idx = script.find("trap 'shred -u")
    trap_clear_idx = script.find("trap - EXIT")
    assert init_idx >= 0 and create_idx >= 0
    assert env_capture_idx >= 0, "wait-script missing env-capture redirect"
    assert trap_install_idx >= 0, "wait-script missing EXIT trap on env-file"
    assert trap_clear_idx >= 0, "wait-script missing trap clear after success"
    assert init_idx < trap_install_idx < env_capture_idx < create_idx < trap_clear_idx, (
        "EXIT trap must be installed BEFORE the env-capture redirect (and "
        "before mngr create) and cleared AFTER successful shred"
    )


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
    """PostToolUse hook destroys the child and cleans up per-tool_use_id state files.

    The hook emits no stdout: output substitution for the parent's Task
    tool_result happens via Haiku's own final reply (the wait-script in
    hooks/spawn.py prints the body and Haiku echoes it verbatim), not via
    this hook -- Claude Code's PostToolUse ``updatedToolOutput`` is
    MCP-only and does not apply to built-in tools.
    """
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    tid = "toolu_xyz"
    map_file = state_dir / "subagent_map" / f"{tid}.json"
    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    watermark_file = state_dir / "proxy_commands" / f"watermark-{tid}"
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
    # Simulate a leaked watermark from a SIGKILL'd / crashed wait-script:
    # subagent_wait normally deletes it on END_TURN, but PostToolUse must
    # also clean it up so orphaned watermarks don't accumulate.
    watermark_file.write_text("4242")

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    # Child is STOPPED in mngr list -> safe to destroy.
    cleanup_hook.run(
        stdin_buffer,
        destroy_callable=fake_destroy,
        list_callable=_list_returns(_fake_list_with_state("foo-bar", AgentLifecycleState.STOPPED)),
    )

    # PostToolUse on the built-in Task tool cannot override tool_result --
    # updatedToolOutput is MCP-only. The hook now emits no JSON; the
    # subagent end-turn text reaches the parent via Haiku's own final
    # reply (see hooks/spawn.py wait-script + mngr-proxy.agent.md).
    # Result file remains on disk for diagnostics? No: the hook still
    # cleans up side files because they are no longer needed once Haiku
    # has captured the content from the wait-script's stdout.
    assert not map_file.exists()
    assert not result_file.exists()
    assert not prompt_file.exists()
    assert not watermark_file.exists()

    # Destroy was requested exactly once with the target name.
    assert len(destroy_calls) == 1
    target, log_path = destroy_calls[0]
    assert target == "foo-bar"
    assert log_path == state_dir / "subagent_destroy.log"


def test_rewrite_missing_result_preserves_subagent(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """When result_file is missing, subagent_wait never observed END_TURN
    (Haiku gave up early -- timeout, hallucinated permission dialog,
    retry cap, etc.). The depth-1 child is likely still RUNNING; we
    must NOT destroy it on the parent's PostToolUse, or we throw away
    real work the user could have recovered via `mngr connect`.

    The hook also retains the map_file and per-tid sidefiles in this
    case so on_before_agent_destroy / SessionStart-reaper can still
    pick the child up later.
    """
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

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
    prompt_file = state_dir / "subagent_prompts" / f"{tid}.md"
    prompt_file.write_text("original prompt")
    # No result_file -- simulating Haiku give-up.

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    # Even with the child reported STOPPED in mngr list, missing
    # result_file alone is enough to preserve.
    cleanup_hook.run(
        stdin_buffer,
        destroy_callable=fake_destroy,
        list_callable=_list_returns(_fake_list_with_state(target_name, AgentLifecycleState.STOPPED)),
    )

    # Critical: child is preserved.
    assert destroy_calls == []
    # Critical: state files are preserved so the user / reaper can
    # find the orphan later.
    assert map_file.exists()
    assert prompt_file.exists()


@pytest.mark.parametrize(
    "live_state",
    [AgentLifecycleState.RUNNING, AgentLifecycleState.WAITING],
    ids=["running", "waiting"],
)
def test_rewrite_live_lifecycle_preserves_subagent(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
    live_state: AgentLifecycleState,
) -> None:
    """Even when result_file IS present, a child still in RUNNING /
    WAITING must be preserved -- catches edge cases where subagent_wait
    saw an early end_turn but the child legitimately re-entered (e.g.
    waiting for a permission prompt resolution that will produce more
    work).
    """
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    tid = "toolu_live"
    target_name = "foo-live-target"
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
    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    result_file.write_text("intermediate end-turn text")

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    cleanup_hook.run(
        stdin_buffer,
        destroy_callable=fake_destroy,
        list_callable=_list_returns(_fake_list_with_state(target_name, live_state)),
    )
    assert destroy_calls == [], f"child in {live_state} must be preserved, not destroyed"
    # State files are retained in the live-preserve path so the
    # SessionStart reaper / on_before_agent_destroy cascade can
    # find the orphan later.
    assert map_file.exists()
    assert result_file.exists()


def test_rewrite_preserves_subagent_when_mngr_list_errors(
    tmp_path: Path,
    clean_env: pytest.MonkeyPatch,
) -> None:
    """A transient mngr-list failure must not destroy an in-flight child.

    rewrite.run() documents the policy as "either signal alive wins; both
    must say done to destroy." When list_callable returns None (mngr list
    timed out / errored), the lifecycle signal is unknown -- treating it
    as 'safely dead' would let a flaky listing call destroy a still-running
    subagent. Conservative behavior is to preserve.
    """
    state_dir = tmp_path / "state"
    for sub in ("subagent_map", "subagent_results", "subagent_prompts", "proxy_commands"):
        (state_dir / sub).mkdir(parents=True)
    clean_env.setenv("MNGR_AGENT_STATE_DIR", str(state_dir))

    tid = "toolu_listfail"
    target_name = "foo-listfail-target"
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
    result_file = state_dir / "subagent_results" / f"{tid}.txt"
    result_file.write_text("intermediate end-turn text")

    destroy_calls: list[tuple[str, Path]] = []

    def fake_destroy(target_name: str, log_path: Path) -> None:
        destroy_calls.append((target_name, log_path))

    stdin_buffer = io.StringIO(json.dumps({"tool_use_id": tid, "tool_response": "ignored"}))
    cleanup_hook.run(
        stdin_buffer,
        destroy_callable=fake_destroy,
        list_callable=_list_returns(None),
    )

    assert destroy_calls == [], "mngr-list failure must not destroy in-flight child"
    assert map_file.exists()
    assert result_file.exists()


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
    cleanup_hook.run(
        stdin_buffer,
        destroy_callable=fake_destroy,
        list_callable=_list_returns({}),
    )
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
    watermark_file = state_dir / "proxy_commands" / f"watermark-{tid}"
    watermark_file.write_text("100")

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
    assert not watermark_file.exists()
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
