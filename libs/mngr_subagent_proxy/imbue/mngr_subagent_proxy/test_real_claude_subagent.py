"""Real-Claude end-to-end release tests for the mngr_subagent_proxy plugin.

These tests spawn an actual local Claude Code agent via the ``mngr create``
CLI, send it a prompt that unambiguously triggers the ``Task`` tool, and
verify that the plugin's PreToolUse hook intercepts the invocation and
spawns a mngr-managed subagent (named ``<parent>--subagent-<slug>-<tid>``)
instead of Claude's native nested Agent loop.

These are slow, environment-heavy tests. They are marked with
``@pytest.mark.release`` and are therefore NOT run in CI. Invoke manually:

    cd <repo_root>
    just test libs/mngr_subagent_proxy/imbue/mngr_subagent_proxy/test_real_claude_subagent.py::test_task_tool_spawns_mngr_subagent

Prerequisites to actually execute (the tests skip gracefully when absent):
- ``claude`` binary on PATH (Claude Code CLI, v2.x).
- Working Claude credentials reachable from the subprocess env. Because the
  autouse ``setup_test_mngr_env`` fixture redirects ``HOME`` to a pytest
  tmp dir, the developer's real ``~/.claude.json`` is NOT visible. Supply
  credentials in one of two ways:
    * set ``ANTHROPIC_API_KEY`` in the environment, or
    * set ``MNGR_TEST_REAL_CLAUDE_JSON=$HOME/.claude.json`` so the fixture
      copies the auth-relevant fields into the isolated HOME.
- ``git`` / ``tmux`` / ``jq`` / ``uv`` on PATH (the usual mngr system deps).

These tests shell out to ``uv run mngr``, which in turn spawns a real
Claude process against the Anthropic API, so they cost real tokens. Keep
them short.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from typing import Callable
from typing import Final

import pytest
from pydantic import Field

from imbue.imbue_common.frozen_model import FrozenModel
from imbue.mngr.errors import MngrError
from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.testing import init_git_repo

_CLAUDE_BINARY: Final[str] = "claude"
_DEFAULT_SPAWN_TIMEOUT_SECONDS: Final[float] = 300.0
_DEFAULT_WAIT_TIMEOUT_SECONDS: Final[float] = 240.0
_POLL_INTERVAL_SECONDS: Final[float] = 2.0
_MNGR_LIST_TIMEOUT_SECONDS: Final[float] = 30.0
_DESTROY_TIMEOUT_SECONDS: Final[float] = 120.0
_BANANA_SENTINEL: Final[str] = "BANANA"

# A prompt that unambiguously asks Claude to use the Task tool with a
# specific subagent_type and a terse return payload. We keep it short and
# explicit so the model does the right thing on the first attempt. The
# parent is instructed to echo the child's reply back so we can grep the
# parent transcript for the sentinel.
_GOLDEN_PATH_PROMPT: Final[str] = (
    "Use the Task tool exactly once. Set subagent_type to 'general-purpose'. "
    "Set the prompt to: Say exactly the word BANANA and nothing else, then end your turn. "
    "After the Task tool returns, reply to me with exactly: SUBAGENT_SAID=<their-reply>. "
    "Do not use any other tools."
)

# For the depth-limit test we set MNGR_SUBAGENT_DEPTH>=MNGR_MAX_SUBAGENT_DEPTH
# so the PreToolUse:Agent depth-limit guard in
# imbue.mngr_subagent_proxy.hooks.spawn triggers immediately on the
# parent's first Task call.
_DEPTH_LIMIT_PROMPT: Final[str] = (
    "Use the Task tool exactly once with subagent_type 'general-purpose' "
    "and prompt 'Say hello'. Then tell me what the subagent said."
)

# Prompt for the background-mode test. We tell Claude to call Task with
# run_in_background=true; the proxy's spawn hook flips into "spawn-only"
# mode (see hooks/spawn.py) and Haiku replies immediately with the poll
# handles instead of blocking on the child's end_turn.
_BACKGROUND_TASK_PROMPT: Final[str] = (
    "Use the Task tool exactly once with subagent_type 'general-purpose', "
    "run_in_background set to true, and prompt: 'Say exactly the word BANANA "
    "and nothing else, then end your turn.' "
    "After the Task tool returns (which should happen immediately because "
    "run_in_background=true), reply to me with exactly: TASK_RETURNED=<verbatim "
    "tool_result content>. Do not use any other tools."
)

# Dialog keys to dismiss in the test-HOME's ~/.claude.json so the agent
# starts without blocking dialogs. Mirrors mngr_claude's internal
# _ALL_DIALOGS_DISMISSED.
_ALL_DIALOGS_DISMISSED: Final[dict[str, bool]] = {
    "effortCalloutDismissed": True,
    "hasCompletedOnboarding": True,
    "bypassPermissionsModeAccepted": True,
    "hasAcknowledgedCostThreshold": True,
}


class _MngrSubprocess(FrozenModel):
    """Subprocess-env bundle for invoking ``uv run mngr`` from tests."""

    env: dict[str, str] = Field(description="Environment variables for child mngr processes.")
    cwd: Path = Field(description="Working directory from which to invoke mngr.")
    repo_root: Path = Field(description="Root of the imbue-ai repo (for uv workspace resolution).")

    def run(
        self,
        args: list[str],
        *,
        timeout: float,
    ) -> subprocess.CompletedProcess[str]:
        """Run ``uv run mngr ARGS`` with the test's isolated env."""
        return subprocess.run(
            ["uv", "run", "mngr", *args],
            capture_output=True,
            text=True,
            cwd=self.cwd,
            env=self.env,
            timeout=timeout,
            check=False,
        )

    def list_agents(self) -> list[dict[str, Any]]:
        """Return ``mngr list --format json``'s agents list, or [] on failure."""
        try:
            result = self.run(
                ["list", "--format", "json"],
                timeout=_MNGR_LIST_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return []
        if result.returncode != 0:
            return []
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            return []
        agents = payload.get("agents")
        return agents if isinstance(agents, list) else []


def _real_home_claude_credentials() -> dict[str, Any] | None:
    """Load Claude auth data from ``MNGR_TEST_REAL_CLAUDE_JSON``, if set.

    The autouse ``setup_test_mngr_env`` fixture redirects ``HOME`` to
    tmp_path, so the developer's real ``~/.claude.json`` (with OAuth login)
    becomes invisible to both this test process and any subprocesses it
    spawns. If the developer points ``MNGR_TEST_REAL_CLAUDE_JSON`` at their
    real file, we copy the auth-relevant top-level fields into the
    isolated HOME.
    """
    override_path = os.environ.get("MNGR_TEST_REAL_CLAUDE_JSON")
    if override_path is None:
        return None
    path = Path(override_path)
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _has_required_binaries() -> list[str]:
    """Return the list of missing required binaries (empty means all present)."""
    missing: list[str] = []
    for binary in (_CLAUDE_BINARY, "git", "tmux", "jq", "uv"):
        if shutil.which(binary) is None:
            missing.append(binary)
    return missing


def _skip_reason_for_environment() -> str | None:
    """Compute why this environment can't run real-Claude tests, or None."""
    missing = _has_required_binaries()
    if missing:
        return f"missing required binaries on PATH: {missing}"
    if not (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("MNGR_TEST_REAL_CLAUDE_JSON")):
        return (
            "no Claude credentials reachable from subprocess: set ANTHROPIC_API_KEY "
            "in the env, or set MNGR_TEST_REAL_CLAUDE_JSON=$HOME/.claude.json to "
            "copy your OAuth login into the test-isolated HOME."
        )
    return None


@pytest.fixture
def _skip_if_no_real_claude() -> None:
    """Skip the test unless real Claude + its credentials are available."""
    reason = _skip_reason_for_environment()
    if reason is not None:
        pytest.skip(reason)


@pytest.fixture
def _source_repo(tmp_path: Path) -> Path:
    """Create a small git repo the parent agent will use as its work dir."""
    source = tmp_path / "source"
    source.mkdir()
    (source / "README.md").write_text("# real-claude-subagent test repo\n")
    (source / ".gitignore").write_text(".claude/settings.local.json\n")
    init_git_repo(source, initial_commit=True)
    return source


def _write_trusted_claude_json(home: Path, trusted_dirs: list[Path]) -> None:
    """Write ``<home>/.claude.json`` with dialogs dismissed and trust for given dirs.

    Also copies credentials from ``MNGR_TEST_REAL_CLAUDE_JSON`` if provided,
    preserving only auth-relevant keys so that Claude can actually
    authenticate without dragging in per-project state from the dev's box.
    """
    base: dict[str, Any] = dict(_ALL_DIALOGS_DISMISSED)
    real = _real_home_claude_credentials()
    if real is not None:
        for key in ("primaryApiKey", "oauthAccount", "accounts", "customApiKeyResponses"):
            if key in real:
                base[key] = real[key]
    base["projects"] = {
        str(path.resolve()): {
            "hasTrustDialogAccepted": True,
            "allowedTools": [],
        }
        for path in trusted_dirs
    }
    (home / ".claude.json").write_text(json.dumps(base))


@pytest.fixture
def _mngr_subprocess_env(
    tmp_path: Path,
    temp_host_dir: Path,
    mngr_test_prefix: str,
    mngr_test_root_name: str,
    _source_repo: Path,
) -> _MngrSubprocess:
    """Build a subprocess env that isolates ``mngr`` calls to this test.

    Inherits the current environment (so ANTHROPIC_API_KEY and the uv cache
    are available) but overrides MNGR_HOST_DIR / MNGR_PREFIX / MNGR_ROOT_NAME
    so the test does not touch the developer's real mngr state. Also
    pre-writes a trusted ``~/.claude.json`` in the test-isolated HOME so
    that dialog prompts don't block the agent at startup.
    """
    home_dir = Path(os.environ["HOME"])
    # Safety belt: the autouse setup_test_mngr_env fixture must have set
    # HOME to a tmp path by now. Writing into the real home would be bad.
    # pytest tmp_path lives under /private/var/... on macOS and /tmp/...
    # on Linux; both show up through tmp_path.parent.
    assert str(home_dir).startswith(str(tmp_path.parent)) or str(home_dir).startswith("/private"), (
        f"Refusing to write .claude.json into unexpected HOME={home_dir!r}. "
        f"Expected a pytest tmp_path under {tmp_path.parent!r}."
    )
    _write_trusted_claude_json(home_dir, [_source_repo])

    env = os.environ.copy()
    env["MNGR_HOST_DIR"] = str(temp_host_dir)
    env["MNGR_PREFIX"] = mngr_test_prefix
    env["MNGR_ROOT_NAME"] = mngr_test_root_name
    env.pop("TMUX", None)

    # Disable Modal in the per-test mngr profile -- the env-isolated HOME
    # doesn't have a Modal token, so any `mngr list` call would hit
    # "Modal is not authorized" and fail with returncode 1, masking
    # the real test signal. Local-host provider is all we need here.
    mngr_root = home_dir / ".mngr"
    profile_name = "test-profile"
    profile_dir = mngr_root / "profiles" / profile_name
    profile_dir.mkdir(parents=True, exist_ok=True)
    (mngr_root / "config.toml").write_text(f'profile = "{profile_name}"\n')
    (profile_dir / "settings.toml").write_text("[providers.modal]\nis_enabled = false\n")

    here = Path(__file__).resolve()
    repo_root: Path | None = None
    for parent in here.parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").exists():
            repo_root = parent
            break
    if repo_root is None:
        raise MngrError(f"could not locate repo root from {here!r}")
    return _MngrSubprocess(env=env, cwd=_source_repo, repo_root=repo_root)


def _make_parent_agent_name() -> str:
    """Build a short, unique parent agent name for this test run."""
    return f"sub-proxy-e2e-{uuid.uuid4().hex[:8]}"


def _poll_for_agent_by_name_prefix(
    mngr: _MngrSubprocess,
    prefix: str,
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Poll ``mngr list`` until any agent whose name starts with ``prefix`` appears."""

    def producer() -> dict[str, Any] | None:
        for agent in mngr.list_agents():
            name = agent.get("name")
            if isinstance(name, str) and name.startswith(prefix):
                return agent
        return None

    value, _, _ = poll_for_value(producer, timeout=timeout, poll_interval=_POLL_INTERVAL_SECONDS)
    return value


def _poll_for_agent_state(
    mngr: _MngrSubprocess,
    agent_name: str,
    predicate: Callable[[dict[str, Any]], bool],
    *,
    timeout: float,
) -> dict[str, Any] | None:
    """Poll ``mngr list`` until ``predicate(agent_dict)`` is True for ``agent_name``.

    Returns the matching agent dict if the predicate became True, else the
    last observation of the agent (or None if it never appeared).
    """
    last_seen_box: dict[str, dict[str, Any] | None] = {"agent": None}

    def producer() -> dict[str, Any] | None:
        for agent in mngr.list_agents():
            if agent.get("name") != agent_name:
                continue
            last_seen_box["agent"] = agent
            if predicate(agent):
                return agent
            return None
        return None

    value, _, _ = poll_for_value(producer, timeout=timeout, poll_interval=_POLL_INTERVAL_SECONDS)
    if value is not None:
        return value
    return last_seen_box["agent"]


def _is_waiting_end_of_turn(agent: dict[str, Any]) -> bool:
    """Return True if ``mngr list`` reports the agent as WAITING with END_OF_TURN."""
    if agent.get("state") != "WAITING":
        return False
    plugin = agent.get("plugin")
    if not isinstance(plugin, dict):
        return False
    claude_plugin = plugin.get("claude")
    if not isinstance(claude_plugin, dict):
        return False
    return claude_plugin.get("waiting_reason") == "END_OF_TURN"


def _destroy_agents_quietly(mngr: _MngrSubprocess, names: Iterator[str]) -> None:
    """Best-effort destroy of the given agents; swallow all errors."""
    for name in names:
        try:
            mngr.run(["destroy", name, "--force"], timeout=_DESTROY_TIMEOUT_SECONDS)
        except (subprocess.TimeoutExpired, OSError):
            # The session_cleanup autouse fixture is a safety net; don't
            # let teardown noise mask the actual test failure.
            pass


def _agent_settings_local_json(agent_name: str, work_dir: Path) -> str:
    """Return the contents of <work_dir>/.claude/settings.local.json or '<missing>'."""
    settings = work_dir / ".claude" / "settings.local.json"
    if not settings.is_file():
        return f"<settings.local.json missing for {agent_name} at {settings}>"
    try:
        return settings.read_text(errors="replace")
    except OSError as e:
        return f"<read failed for {settings}: {e}>"


def _diagnose_subagent_proxy_failure(
    mngr: _MngrSubprocess,
    parent_name: str,
    parent_work_dir: Path,
    host_dir: Path,
) -> str:
    """Build a diagnostic report for subagent-proxy failure paths.

    Called both when the expected mngr subagent never appeared AND when
    the parent agent failed to reach WAITING/END_OF_TURN after the
    subagent finished. Captures:
    - whether ``mngr list`` itself works (and its stderr if it doesn't).
    - the parent's settings.local.json (does it have PreToolUse:Agent?).
    - the tail of the parent's transcript (did Claude actually call Task?).

    Best-effort: this helper is only ever invoked on the failure path, so
    it must never raise. Subprocess errors are caught and reported inline.
    """
    parts: list[str] = ["", "=== DIAGNOSTIC ==="]

    try:
        plugins_check = mngr.run(
            ["list", "--format", "json"],
            timeout=_MNGR_LIST_TIMEOUT_SECONDS,
        )
        parts.append(f"mngr list returncode: {plugins_check.returncode}")
        if plugins_check.returncode != 0:
            parts.append(f"mngr list stderr (truncated to 2000 chars): {plugins_check.stderr[:2000]}")
    except (subprocess.TimeoutExpired, OSError) as e:
        parts.append(f"mngr list failed to run: {e!r}")

    settings_text = _agent_settings_local_json(parent_name, parent_work_dir)
    has_pretooluse_agent = '"matcher": "Agent"' in settings_text or '"matcher":"Agent"' in settings_text
    parts.append(f"settings.local.json has PreToolUse:Agent matcher: {has_pretooluse_agent}")
    parts.append("settings.local.json (truncated to 4000 chars):")
    parts.append(settings_text[:4000])

    parent = next((a for a in mngr.list_agents() if a.get("name") == parent_name), None)
    if parent is not None:
        transcript = _agent_transcript_text(parent, host_dir)
        has_task = '"name":"Task"' in transcript or '"name": "Task"' in transcript
        parts.append(f"parent transcript has Task tool_use: {has_task}")
        parts.append(f"transcript length: {len(transcript)} chars")
        parts.append("transcript tail (last 3000 chars):")
        parts.append(transcript[-3000:])
    else:
        parts.append("parent agent not found in mngr list (already destroyed?)")
    return "\n".join(parts)


def _agent_transcript_text(agent: dict[str, Any], host_dir: Path) -> str:
    """Concatenate all assistant/user text from the agent's transcript JSONL files.

    Returns "" if the transcript directory can't be located. Best-effort
    grep target, not a structured parse.
    """
    agent_id = agent.get("id")
    if not isinstance(agent_id, str):
        return ""
    projects_root = host_dir / "agents" / agent_id / "plugin" / "claude" / "anthropic" / "projects"
    if not projects_root.is_dir():
        return ""
    parts: list[str] = []
    for jsonl in projects_root.rglob("*.jsonl"):
        try:
            parts.append(jsonl.read_text(errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def _create_parent_claude_agent(
    mngr: _MngrSubprocess,
    agent_name: str,
    prompt: str,
    *,
    extra_env: dict[str, str] | None = None,
    claude_args: tuple[str, ...] = ("--dangerously-skip-permissions",),
) -> subprocess.CompletedProcess[str]:
    """Create a Claude agent via the ``mngr create`` CLI with the given prompt.

    Uses ``--transfer=none`` so the agent shares the source repo in-place,
    ``--no-connect`` so we don't attach a TUI, ``--no-ensure-clean`` because
    provisioning may touch settings.local.json, and (by default)
    ``--dangerously-skip-permissions`` (after ``--``) so Claude never blocks
    on permission dialogs. Trust is pre-seeded via ``~/.claude.json``.

    ``claude_args`` lets a caller substitute a different set of flags --
    e.g. ``--permission-mode plan`` for plan-mode propagation tests, where
    bypassPermissions and plan are mutually exclusive permission modes.
    """
    cmd_args: list[str] = [
        "create",
        f"{agent_name}@.local",
        "--type",
        "claude",
        "--transfer",
        "none",
        "--no-connect",
        "--no-ensure-clean",
        "--message",
        prompt,
    ]
    if extra_env:
        for key, value in extra_env.items():
            cmd_args.extend(["--env", f"{key}={value}"])
    cmd_args.append("--")
    cmd_args.extend(claude_args)
    return mngr.run(cmd_args, timeout=_DEFAULT_SPAWN_TIMEOUT_SECONDS)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_task_tool_spawns_mngr_subagent(
    _skip_if_no_real_claude: None,
    _mngr_subprocess_env: _MngrSubprocess,
    _source_repo: Path,
    temp_host_dir: Path,
) -> None:
    """Golden path: the Task tool is intercepted and an mngr subagent is spawned.

    End-to-end verification that:
    1. The subagent-proxy plugin hooks are installed on the parent Claude agent.
    2. When the parent calls the Task tool, the PreToolUse hook rewrites it
       to spawn an mngr-managed subagent named
       ``<parent>--subagent-<slug>-<tid>``.
    3. The subagent runs Claude, replies with the BANANA sentinel, and
       reaches WAITING / END_OF_TURN.
    4. The parent resumes, observes the subagent's output, and itself
       reaches WAITING / END_OF_TURN.
    """
    mngr = _mngr_subprocess_env
    parent_name = _make_parent_agent_name()
    created_agents: list[str] = [parent_name]

    try:
        create_result = _create_parent_claude_agent(mngr, parent_name, _GOLDEN_PATH_PROMPT)
        assert create_result.returncode == 0, (
            f"mngr create failed (exit={create_result.returncode})\n"
            f"stderr:\n{create_result.stderr}\nstdout:\n{create_result.stdout}"
        )

        subagent_prefix = f"{parent_name}--subagent-"
        subagent = _poll_for_agent_by_name_prefix(
            mngr,
            subagent_prefix,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if subagent is None:
            diagnostics = _diagnose_subagent_proxy_failure(mngr, parent_name, _source_repo, temp_host_dir)
            pytest.fail(
                f"No mngr-managed subagent with prefix {subagent_prefix!r} appeared within "
                f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. This strongly suggests the PreToolUse "
                f"hook did not fire, or Claude never called the Task tool."
                f"{diagnostics}"
            )
        subagent_name = subagent["name"]
        created_agents.append(subagent_name)

        final_sub = _poll_for_agent_state(
            mngr,
            subagent_name,
            _is_waiting_end_of_turn,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        assert final_sub is not None and _is_waiting_end_of_turn(final_sub), (
            f"Subagent {subagent_name!r} never reached WAITING/END_OF_TURN within "
            f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. Last seen: {final_sub!r}"
        )

        final_parent = _poll_for_agent_state(
            mngr,
            parent_name,
            _is_waiting_end_of_turn,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if final_parent is None or not _is_waiting_end_of_turn(final_parent):
            diagnostics = _diagnose_subagent_proxy_failure(mngr, parent_name, _source_repo, temp_host_dir)
            pytest.fail(
                f"Parent {parent_name!r} never reached WAITING/END_OF_TURN within "
                f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. Subagent finished but parent is "
                f"stuck -- likely the PostToolUse hook didn't substitute the result, "
                f"or Haiku didn't end its turn after the wait-script returned DONE."
                f"{diagnostics}"
            )

        # Content check: the parent transcript should mention BANANA if the
        # subagent's reply was actually wired back in.
        transcript = _agent_transcript_text(final_parent, temp_host_dir)
        assert _BANANA_SENTINEL in transcript, (
            f"Sentinel {_BANANA_SENTINEL!r} not found in parent transcripts. "
            f"This may mean the PostToolUse hook did not rewrite the Task "
            f"result, or the subagent never emitted the sentinel. "
            f"Transcript length: {len(transcript)} chars."
        )
    finally:
        _destroy_agents_quietly(mngr, iter(created_agents))


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_depth_limit_denies_task(
    _skip_if_no_real_claude: None,
    _mngr_subprocess_env: _MngrSubprocess,
) -> None:
    """At depth >= max_depth, the hook denies the Task tool entirely.

    The spawn hook emits ``permissionDecision: deny`` with a
    ``permissionDecisionReason`` mentioning the depth limit. Claude sees
    the denial and does not invoke Task. Crucially, **no mngr-managed
    subagent is created**. This test verifies that property.
    """
    mngr = _mngr_subprocess_env
    parent_name = _make_parent_agent_name()
    created_agents: list[str] = [parent_name]

    try:
        create_result = _create_parent_claude_agent(
            mngr,
            parent_name,
            _DEPTH_LIMIT_PROMPT,
            extra_env={
                "MNGR_SUBAGENT_DEPTH": "3",
                "MNGR_MAX_SUBAGENT_DEPTH": "3",
            },
        )
        assert create_result.returncode == 0, (
            f"mngr create failed (exit={create_result.returncode})\n"
            f"stderr:\n{create_result.stderr}\nstdout:\n{create_result.stdout}"
        )

        # Wait for the parent to finish its turn. We don't care exactly how
        # it finishes -- only that no mngr subagent is created.
        final_parent = _poll_for_agent_state(
            mngr,
            parent_name,
            _is_waiting_end_of_turn,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        assert final_parent is not None, (
            f"Parent {parent_name!r} never appeared in mngr list. Last seen: {final_parent!r}"
        )

        subagent_prefix = f"{parent_name}--subagent-"
        for agent in mngr.list_agents():
            name = agent.get("name", "")
            assert not (isinstance(name, str) and name.startswith(subagent_prefix)), (
                f"Depth-limited parent should NOT have spawned an mngr subagent, but found: {name!r}"
            )
    finally:
        _destroy_agents_quietly(mngr, iter(created_agents))


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_task_run_in_background_returns_immediately(
    _skip_if_no_real_claude: None,
    _mngr_subprocess_env: _MngrSubprocess,
    _source_repo: Path,
    temp_host_dir: Path,
) -> None:
    """``run_in_background: true`` makes Task return poll handles immediately.

    End-to-end: verifies the spawn hook's background path
    (``hooks/spawn.py`` ``--spawn-only`` branch). Specifically:

    1. The parent's Task call returns to the parent BEFORE the child
       reaches its own end_turn -- distinguishable because the parent
       sees the poll-handle text we synthesize in
       ``hooks/spawn.py:new_prompt`` for the background branch
       (``mngr connect <name>`` / ``mngr transcript <name>``), not the
       child's actual reply body.
    2. The mngr-managed child IS spawned and runs the actual prompt to
       completion (reaches WAITING / END_OF_TURN with the real reply
       in its transcript).
    3. The parent reaches WAITING / END_OF_TURN itself -- the
       background path does not block the parent.
    """
    mngr = _mngr_subprocess_env
    parent_name = _make_parent_agent_name()
    created_agents: list[str] = [parent_name]

    try:
        create_result = _create_parent_claude_agent(mngr, parent_name, _BACKGROUND_TASK_PROMPT)
        assert create_result.returncode == 0, (
            f"mngr create failed (exit={create_result.returncode})\n"
            f"stderr:\n{create_result.stderr}\nstdout:\n{create_result.stdout}"
        )

        subagent_prefix = f"{parent_name}--subagent-"
        subagent = _poll_for_agent_by_name_prefix(
            mngr,
            subagent_prefix,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if subagent is None:
            diagnostics = _diagnose_subagent_proxy_failure(mngr, parent_name, _source_repo, temp_host_dir)
            pytest.fail(
                f"No mngr-managed subagent with prefix {subagent_prefix!r} appeared within "
                f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. The PreToolUse hook did not spawn the "
                f"background child."
                f"{diagnostics}"
            )
        subagent_name = subagent["name"]
        created_agents.append(subagent_name)

        # The parent must reach WAITING/END_OF_TURN. Its tool_result for the
        # Task call is the synthetic poll-handle text, NOT the child's reply,
        # so the parent doesn't block on the child's end_turn.
        final_parent = _poll_for_agent_state(
            mngr,
            parent_name,
            _is_waiting_end_of_turn,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if final_parent is None or not _is_waiting_end_of_turn(final_parent):
            diagnostics = _diagnose_subagent_proxy_failure(mngr, parent_name, _source_repo, temp_host_dir)
            pytest.fail(
                f"Parent {parent_name!r} never reached WAITING/END_OF_TURN within "
                f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. Background mode should NOT block "
                f"the parent on the child's end_turn."
                f"{diagnostics}"
            )

        # The parent's transcript should contain the poll-handle text we
        # synthesized in spawn.py's background branch -- specifically the
        # `mngr transcript <name>` line. This confirms the proxy returned
        # the poll handles (not the child's actual reply) as the
        # tool_result.
        parent_transcript = _agent_transcript_text(final_parent, temp_host_dir)
        expected_handle = f"mngr transcript {subagent_name}"
        assert expected_handle in parent_transcript, (
            f"Parent transcript missing poll-handle text {expected_handle!r}. "
            f"This means the proxy did NOT return poll handles to the parent on "
            f"run_in_background=true; it likely fell through to the foreground "
            f"path or Haiku synthesized something else. Transcript length: "
            f"{len(parent_transcript)} chars."
        )

        # The child should still reach end_turn under normal mngr lifecycle
        # -- background mode just means the parent doesn't block waiting for
        # it. The child runs to completion regardless.
        final_sub = _poll_for_agent_state(
            mngr,
            subagent_name,
            _is_waiting_end_of_turn,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        assert final_sub is not None and _is_waiting_end_of_turn(final_sub), (
            f"Background subagent {subagent_name!r} never reached "
            f"WAITING/END_OF_TURN within {_DEFAULT_WAIT_TIMEOUT_SECONDS}s. "
            f"Last seen: {final_sub!r}"
        )

        # The child's own transcript should contain the BANANA reply. The
        # parent never sees this directly (it got poll handles); the user
        # would observe it via `mngr transcript <child>`.
        child_transcript = _agent_transcript_text(final_sub, temp_host_dir)
        assert _BANANA_SENTINEL in child_transcript, (
            f"Child {subagent_name!r} did not emit {_BANANA_SENTINEL!r}. "
            f"Transcript length: {len(child_transcript)} chars."
        )
    finally:
        _destroy_agents_quietly(mngr, iter(created_agents))


# Plan-mode regression: a parent in plan mode delegates research-only work
# via Task. The subagent it spawns MUST inherit plan mode -- otherwise the
# read-only guarantee leaks (the subagent could mutate state the parent
# itself was forbidden from touching). Plan mode in Claude Code is
# selected via ``--permission-mode plan`` and is mutually exclusive with
# ``bypassPermissions`` (i.e. ``--dangerously-skip-permissions``), so
# this test substitutes the parent's CLI flags rather than appending.
_PLAN_MODE_PROMPT: Final[str] = (
    "Use the Task tool exactly once. Set subagent_type to 'general-purpose'. "
    "Set the prompt to: Say exactly the word BANANA and nothing else, then end your turn. "
    "Then end your own turn. Do not use any other tools."
)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_plan_mode_propagates_to_subagent(
    _skip_if_no_real_claude: None,
    _mngr_subprocess_env: _MngrSubprocess,
    _source_repo: Path,
    temp_host_dir: Path,
) -> None:
    """A parent in plan mode spawns subagents that also run in plan mode.

    Plan mode (``--permission-mode plan``) restricts a Claude Code agent
    to read-only tools: no Edit, no Write, no mutating Bash. When such a
    parent delegates research via the Task tool, the spawned subagent
    MUST inherit plan mode -- otherwise the subagent can freely mutate
    state that the parent itself was forbidden from touching, defeating
    the read-only guarantee plan mode is supposed to provide.

    The proxy spawns its child via ``mngr create --type mngr-proxy-child``
    (see ``hooks/spawn.py:build_wait_script``), so the child's claude
    command line -- exposed by ``mngr list --format json`` as the
    ``command`` field on AgentDetails -- is the authoritative observable
    for plan-mode propagation. We assert that command contains
    ``--permission-mode plan`` (in either argv-pair or ``=``-joined form).
    """
    mngr = _mngr_subprocess_env
    parent_name = _make_parent_agent_name()
    created_agents: list[str] = [parent_name]

    try:
        create_result = _create_parent_claude_agent(
            mngr,
            parent_name,
            _PLAN_MODE_PROMPT,
            claude_args=("--permission-mode", "plan"),
        )
        assert create_result.returncode == 0, (
            f"mngr create failed (exit={create_result.returncode})\n"
            f"stderr:\n{create_result.stderr}\nstdout:\n{create_result.stdout}"
        )

        subagent_prefix = f"{parent_name}--subagent-"
        subagent = _poll_for_agent_by_name_prefix(
            mngr,
            subagent_prefix,
            timeout=_DEFAULT_WAIT_TIMEOUT_SECONDS,
        )
        if subagent is None:
            diagnostics = _diagnose_subagent_proxy_failure(mngr, parent_name, _source_repo, temp_host_dir)
            pytest.fail(
                f"No mngr-managed subagent with prefix {subagent_prefix!r} appeared within "
                f"{_DEFAULT_WAIT_TIMEOUT_SECONDS}s. Plan-mode parent never reached the "
                f"Task tool, or the spawn hook did not fire."
                f"{diagnostics}"
            )
        subagent_name = subagent["name"]
        created_agents.append(subagent_name)

        child_command = subagent.get("command")
        assert isinstance(child_command, str) and child_command, (
            f"Spawned subagent {subagent_name!r} is missing a 'command' field in mngr list output: {subagent!r}"
        )
        # Accept both argv-pair (``--permission-mode plan``) and equals-joined
        # (``--permission-mode=plan``) forms; mngr_claude assembles cli_args
        # via shell join, so either survives round-trip into the command field.
        has_plan_flag = "--permission-mode plan" in child_command or "--permission-mode=plan" in child_command
        assert has_plan_flag, (
            f"Subagent {subagent_name!r} did NOT inherit plan mode. Its claude "
            f"command line is: {child_command!r}. Plan-mode's read-only guarantee "
            f"requires the spawn hook to propagate ``--permission-mode plan`` to "
            f"the child's mngr-create call (see hooks/spawn.py)."
        )
    finally:
        _destroy_agents_quietly(mngr, iter(created_agents))
