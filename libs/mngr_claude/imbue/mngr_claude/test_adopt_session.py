"""Release tests for ``mngr create --adopt``.

Verifies end-to-end that a new mngr-managed claude agent created with
``--adopt`` actually resumes from the source session: the destination
agent's claude process must receive the prior conversation as context and be
able to recall a unique secret that was planted in the source session.

Covers adopting a session produced by the vanilla ``claude`` CLI with no mngr
involvement (``test_adopt_session_brings_context_from_vanilla_claude_session``),
whose JSONL lives under ``~/.claude/projects/<encoded-cwd>/`` and is adopted by
bare session id. Adoption from an mngr-managed agent's own session is covered
end-to-end by the shared release harness (``test_claude_agent_e2e.py``), which
preserves a destroyed agent's session and adopts it into a fresh worktree.

These are release tests; release tests do not run in CI. To run manually::

    PYTEST_MAX_DURATION_SECONDS=1500 ANTHROPIC_API_KEY=sk-ant-... \\
        uv run pytest --no-cov --cov-fail-under=0 -n 0 -m release \\
        libs/mngr_claude/imbue/mngr_claude/test_adopt_session.py
"""

import os
import shlex
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_for_value
from imbue.mngr.utils.polling import wait_for
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import setup_claude_trust_config_for_subprocess
from imbue.mngr_claude.claude_config import encode_claude_project_dir_name

# Phrasing the prompts so the answer is unambiguous makes the assertion
# robust to model verbosity. The secret is a UUID, so an accidental match
# in the model's pre-existing knowledge is effectively impossible.
_SEED_PROMPT_TEMPLATE = (
    "Please remember this exact value, which I will ask you to recall later: "
    "the secret answer is {secret}. Acknowledge by repeating the secret answer."
)
_RECALL_PROMPT_TEMPLATE = (
    "Earlier in this conversation I told you the secret answer. "
    "Please respond with just the secret answer, exactly as I gave it to you."
)

_PROVISION_TIMEOUT_SECONDS = 600
_VANILLA_CLAUDE_TIMEOUT_SECONDS = 180
_RESPONSE_TIMEOUT_SECONDS = 240
_DESTROY_TIMEOUT_SECONDS = 120


def _have_claude_credentials() -> bool:
    """Skip-guard: a real ``claude`` binary and ANTHROPIC_API_KEY are required."""
    return shutil.which("claude") is not None and bool(os.environ.get("ANTHROPIC_API_KEY"))


pytestmark = pytest.mark.skipif(
    not _have_claude_credentials(),
    reason="Release test requires ANTHROPIC_API_KEY in the environment and `claude` on PATH.",
)


def _make_git_work_dir(parent: Path, name: str) -> Path:
    """Create a fresh git work-dir under ``parent`` with ``.gitignore`` committed.

    ``mngr create`` requires the source to be a git repo with the claude
    settings.local.json gitignored.
    """
    work_dir = parent / name
    init_git_repo(work_dir, initial_commit=True)
    (work_dir / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git_command(work_dir, "add", ".gitignore")
    run_git_command(work_dir, "commit", "-m", "add gitignore")
    return work_dir


@pytest.fixture
def source_work_dir(tmp_path: Path) -> Path:
    """Work directory used by the source-session producer."""
    return _make_git_work_dir(tmp_path, "source-work")


@pytest.fixture
def dest_work_dir(tmp_path: Path) -> Path:
    """Work directory used by the destination (adopting) agent.

    Distinct from ``source_work_dir`` to ensure the test exercises the
    cross-cwd rehoming logic that ``on_after_provisioning`` performs at
    ``plugin.py:1972``.
    """
    return _make_git_work_dir(tmp_path, "dest-work")


@pytest.fixture
def trusted_subprocess_env(
    source_work_dir: Path,
    dest_work_dir: Path,
    tmp_path: Path,
) -> dict[str, str]:
    """Trust both work_dirs and disable remote providers for subprocess invocations.

    Without trust:

    * ``mngr create`` raises ``ClaudeDirectoryNotTrustedError`` at
      ``plugin.py:1564`` when running locally without ``--yes`` /
      ``auto_dismiss_dialogs``.
    * ``claude`` itself prompts for trust on the destination agent's
      interactive session.

    Without disabling Modal/Docker, ``mngr message`` enumerates providers
    and tries to create a Modal environment using the autouse fixture's
    test prefix (``mngr_<test_id>-``), which Modal rejects because Modal
    test environments must start with ``mngr_test-``.

    The autouse ``setup_test_mngr_env`` fixture has already redirected
    ``HOME`` to a tmp dir, so the helper writes to that tmp dir's
    ``.claude.json`` rather than the developer's real one.
    """
    env = setup_claude_trust_config_for_subprocess(
        trusted_paths=[source_work_dir.resolve(), dest_work_dir.resolve()],
    )
    project_config_dir = tmp_path / ".mngr-adopt-test"
    project_config_dir.mkdir(parents=True, exist_ok=True)
    (project_config_dir / "settings.local.toml").write_text(
        "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
    )
    env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)
    return env


def _run(
    args: list[str],
    env: dict[str, str],
    cwd: Path | None = None,
    timeout: float = 120.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess with capture and a default timeout.

    Default behaviour fails the test on non-zero exit by including
    stdout/stderr in the assertion message; pass ``check=False`` to inspect
    a failure without raising.
    """
    result = subprocess.run(args, env=env, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise AssertionError(
            f"Command failed: {' '.join(args)}\n"
            f"  exit: {result.returncode}\n"
            f"  stdout:\n{result.stdout}\n"
            f"  stderr:\n{result.stderr}"
        )
    return result


def _create_vanilla_claude_session(work_dir: Path, secret: str, env: dict[str, str]) -> tuple[str, Path]:
    """Plant a session via the vanilla ``claude`` CLI.

    ``claude`` only persists session JSONLs when stdout is a TTY, so we
    launch it inside a tmux session (``TMUX_TMPDIR`` is already isolated by
    the autouse fixture). ``ANTHROPIC_API_KEY`` is inlined into the tmux
    command because the tmux server inherits its env from when it was
    started, not from the calling shell.

    Returns ``(session_id, jsonl_path)``.
    """
    seed_prompt = _SEED_PROMPT_TEMPLATE.format(secret=secret)
    home = Path(env["HOME"])
    api_key = env["ANTHROPIC_API_KEY"]

    encoded = encode_claude_project_dir_name(work_dir.resolve())
    project_dir = home / ".claude" / "projects" / encoded
    session_name = f"adopt-test-vanilla-{get_short_random_string()}"
    out_file = work_dir / f".out-{session_name}.txt"
    done_marker = work_dir / f".done-{session_name}"
    inner_cmd = (
        f"cd {shlex.quote(str(work_dir))} && "
        f"ANTHROPIC_API_KEY={shlex.quote(api_key)} HOME={shlex.quote(str(home))} "
        f"claude --dangerously-skip-permissions --print {shlex.quote(seed_prompt)} "
        f"> {shlex.quote(str(out_file))} 2>&1; "
        f"touch {shlex.quote(str(done_marker))}"
    )
    _run(
        ["tmux", "new-session", "-d", "-s", session_name, inner_cmd],
        env=env,
        timeout=10.0,
    )
    try:
        try:
            wait_for(
                done_marker.exists,
                timeout=float(_VANILLA_CLAUDE_TIMEOUT_SECONDS),
                poll_interval=2.0,
                error_message=(f"vanilla claude did not finish within {_VANILLA_CLAUDE_TIMEOUT_SECONDS}s"),
            )
        except TimeoutError as exc:
            output = out_file.read_text() if out_file.exists() else "(no output)"
            raise AssertionError(f"{exc}. output:\n{output}") from exc
    finally:
        subprocess.run(["tmux", "kill-session", "-t", session_name], env=env, capture_output=True)

    sessions = list(project_dir.glob("*.jsonl")) if project_dir.exists() else []
    output = out_file.read_text() if out_file.exists() else "(no output)"
    assert len(sessions) == 1, (
        f"Expected exactly one session JSONL under {project_dir}, "
        f"found {len(sessions)}: {[s.name for s in sessions]}\n"
        f"claude output:\n{output}"
    )
    jsonl_path = sessions[0]
    return jsonl_path.stem, jsonl_path


def _wait_for_text_in_pane(session_name: str, expected: str, env: dict[str, str], timeout: float) -> str:
    """Poll ``tmux capture-pane`` until ``expected`` shows up; return the matching capture.

    Captures the full scrollback (``-S -9999``) so we don't miss the response
    if it's already scrolled past the visible area by the time we look.
    """
    last_capture: list[str] = [""]

    def _capture_if_match() -> str | None:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-9999"],
            capture_output=True,
            text=True,
            env=env,
        )
        last_capture[0] = result.stdout
        return result.stdout if expected in result.stdout else None

    capture, _, _ = poll_for_value(_capture_if_match, timeout=timeout, poll_interval=2.0)
    if capture is None:
        raise AssertionError(
            f"Did not see {expected!r} in tmux pane {session_name!r} within {timeout}s.\n"
            f"Last capture (tail):\n{last_capture[0][-2000:]}"
        )
    return capture


def _destroy_agent(agent_name: str, env: dict[str, str]) -> None:
    """Best-effort destroy: warn but don't fail the test on cleanup errors."""
    _run(
        ["uv", "run", "mngr", "destroy", agent_name, "--force"],
        env=env,
        timeout=float(_DESTROY_TIMEOUT_SECONDS),
        check=False,
    )


def _verify_adopted_context(
    dest_agent_name: str,
    dest_work_dir: Path,
    adopt_arg: str,
    secret: str,
    env: dict[str, str],
) -> None:
    """Create the destination agent with ``--adopt`` and assert it can recall ``secret``.

    Launches the destination interactively (no ``-p``) so we can drive it
    with ``mngr message`` after startup. ``mngr destroy`` is invoked on the
    way out regardless of success.
    """
    # ``_run`` defaults to ``check=True``, so a non-zero ``mngr create`` exit
    # already fails the test with full stdout/stderr -- create-success is
    # verified structurally here, without coupling to mngr's "Done." log
    # wording. The load-bearing behavioral check is the secret recall in the
    # destination agent's pane below.
    _run(
        [
            "uv",
            "run",
            "mngr",
            "create",
            dest_agent_name,
            "claude",
            "--no-connect",
            "--no-ensure-clean",
            "--yes",
            "--source",
            str(dest_work_dir),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--adopt",
            adopt_arg,
            "--",
            "--dangerously-skip-permissions",
        ],
        env=env,
        timeout=float(_PROVISION_TIMEOUT_SECONDS),
    )
    try:
        _run(
            [
                "uv",
                "run",
                "mngr",
                "message",
                dest_agent_name,
                "--message",
                _RECALL_PROMPT_TEMPLATE,
            ],
            env=env,
            timeout=120.0,
        )
        session_name = f"{env['MNGR_PREFIX']}{dest_agent_name}"
        _wait_for_text_in_pane(session_name, secret, env=env, timeout=float(_RESPONSE_TIMEOUT_SECONDS))
    finally:
        _destroy_agent(dest_agent_name, env)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(_PROVISION_TIMEOUT_SECONDS + _VANILLA_CLAUDE_TIMEOUT_SECONDS + _RESPONSE_TIMEOUT_SECONDS + 60)
def test_adopt_session_brings_context_from_vanilla_claude_session(
    source_work_dir: Path,
    dest_work_dir: Path,
    trusted_subprocess_env: dict[str, str],
) -> None:
    """Adopt a session created by the vanilla ``claude`` CLI; the new agent must recall the secret.

    Source layout: ``$HOME/.claude/projects/<encoded-cwd>/<session_id>.jsonl``.
    Adopt by session ID (``--adopt <id>``).
    """
    secret = uuid.uuid4().hex
    session_id, _ = _create_vanilla_claude_session(source_work_dir, secret, trusted_subprocess_env)

    dest_agent_name = f"adopt-vanilla-{get_short_random_string()}"
    _verify_adopted_context(
        dest_agent_name=dest_agent_name,
        dest_work_dir=dest_work_dir,
        adopt_arg=session_id,
        secret=secret,
        env=trusted_subprocess_env,
    )
