"""Release test: full end-to-end codex agent flow against the real ``codex`` CLI.

This drives the real ``mngr`` CLI in an isolated ``MNGR_HOST_DIR`` and a temporary
``CODEX_HOME`` (seeded with a copy of the user's real ``~/.codex/auth.json`` so it
authenticates without touching or polluting the user's real codex config). It
exercises the whole pipeline a unit test cannot: provision -> launch -> readiness
detection -> message -> the RUNNING/WAITING lifecycle marker -> transcript capture
-> and -- the key milestone -- conversation **resume across stop/start carrying
context**.

It is a ``release`` test (not run in CI) and requires the ``codex`` binary plus a
logged-in ``~/.codex/auth.json``; it is skipped otherwise so it never breaks other
environments. Run locally with::

    uv run pytest libs/mngr_codex/imbue/mngr_codex/test_codex_agent_e2e.py \
        -m release -p no:xdist --no-cov

The model is pinned to a ChatGPT-account-safe slug because codex's default
``*-codex`` model is rejected for ChatGPT-account logins (see the lib README).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from imbue.mngr.utils.polling import poll_until

# Resolved at import time, before the autouse ``setup_test_mngr_env`` fixture
# redirects ``$HOME`` / mutates ``PATH``: the real home (auth source) and the
# real ``codex`` / ``mngr`` executables.
_REAL_HOME = Path.home()
_CODEX_BIN = shutil.which("codex") or next(
    (
        candidate
        for candidate in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", str(_REAL_HOME / ".local/bin/codex"))
        if Path(candidate).exists()
    ),
    None,
)
_MNGR_BIN = shutil.which("mngr")
_REAL_AUTH = _REAL_HOME / ".codex" / "auth.json"
_REAL_MODELS_CACHE = _REAL_HOME / ".codex" / "models_cache.json"

# ChatGPT-account-safe model. codex's compiled default is a ``*-codex`` slug that a
# ChatGPT-account login rejects with a 400, so an unset model would make the agent
# error on its first turn. Update if this account's available models change.
_CODEX_MODEL = "gpt-5.5"

_AGENT = "codexe2e"

pytestmark = [
    pytest.mark.release,
    # Marked ``tmux`` because teardown invokes ``tmux kill-server`` in-process to
    # tear down the agent's private tmux server (the resource guard requires the
    # marker for any in-process tmux use). The server is a throwaway under
    # ``/tmp/mngr-codex-e2e-tmux-*`` (see ``_subprocess_env`` / ``_kill_private_tmux_server``),
    # never the real one this test may run inside.
    pytest.mark.tmux,
    pytest.mark.skipif(
        _CODEX_BIN is None or _MNGR_BIN is None or not _REAL_AUTH.exists(),
        reason="codex CLI not installed, mngr not on PATH, or ~/.codex/auth.json missing (not logged in)",
    ),
]


_TMUX_TMPDIR_PREFIX = "/tmp/mngr-codex-e2e-tmux-"


def _subprocess_env(host_dir: Path, user_codex_home: Path, tmux_tmpdir: Path) -> dict[str, str]:
    """Build the env for the ``mngr`` subprocess: isolated host dir, codex home, and tmux server.

    ``MNGR_HOST_DIR`` segregates the agent's state from the real host. ``CODEX_HOME``
    points provisioning at the seeded throwaway codex home (so the per-agent
    ``auth.json`` symlink and any durable trust write land there, never in the real
    ``~/.codex``). ``PATH`` is extended so the launched agent finds the ``codex``
    binary and ``mngr`` finds itself.

    Critically, ``$TMUX`` is dropped and ``TMUX_TMPDIR`` is pointed at a private
    socket dir so the agent's tmux sessions live on a throwaway server -- never the
    real server this test process may itself be running inside (mirrors the shared
    ``isolate_tmux_server`` helper). Without this, ``mngr`` would inherit the
    caller's ``$TMUX`` and operate on the user's real tmux server.
    """
    assert _CODEX_BIN is not None and _MNGR_BIN is not None
    env = os.environ.copy()
    env["MNGR_HOST_DIR"] = str(host_dir)
    env["CODEX_HOME"] = str(user_codex_home)
    env.pop("TMUX", None)
    env["TMUX_TMPDIR"] = str(tmux_tmpdir)
    extra_path = os.pathsep.join({str(Path(_CODEX_BIN).parent), str(Path(_MNGR_BIN).parent)})
    env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")
    return env


def _kill_private_tmux_server(tmux_tmpdir: Path) -> None:
    """Kill the throwaway tmux server for ``tmux_tmpdir`` (guarded against the real server)."""
    tmpdir_str = str(tmux_tmpdir)
    # Safety: only ever kill a server whose socket lives under our private prefix,
    # so a mis-set path can never take down the user's real tmux server.
    assert tmpdir_str.startswith(_TMUX_TMPDIR_PREFIX), (
        f"refusing to kill-server for unexpected TMUX_TMPDIR {tmpdir_str}"
    )
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmpdir_str
    subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], capture_output=True, env=kill_env)
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


# Disable the remote-provider backends for the whole run: this is a purely local
# agent test, and leaving them enabled makes commands like ``mngr message`` probe
# Modal/Docker (failing if Modal isn't authenticated in this environment). ``-S``
# is a per-command option, so it is injected right after the subcommand.
_PROVIDER_SETTINGS: tuple[str, ...] = (
    "-S",
    "providers.modal.is_enabled=false",
    "-S",
    "providers.docker.is_enabled=false",
)


def _run_mngr(
    env: dict[str, str], cwd: Path, subcommand: str, *rest: str, timeout: float = 180.0
) -> subprocess.CompletedProcess[str]:
    assert _MNGR_BIN is not None
    return subprocess.run(
        [_MNGR_BIN, subcommand, *_PROVIDER_SETTINGS, *rest],
        env=env,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_for_state(env: dict[str, str], cwd: Path, agent: str, state: str, timeout: float = 120.0) -> str:
    """Poll ``mngr list`` until ``agent`` is reported in ``state``; return the last listing."""
    last = {"out": ""}

    def _is_in_state() -> bool:
        last["out"] = _run_mngr(env, cwd, "list", timeout=30.0).stdout
        return any(agent in line and state in line for line in last["out"].splitlines())

    if not poll_until(condition=_is_in_state, timeout=timeout, poll_interval=3.0):
        raise AssertionError(
            f"Agent {agent!r} did not reach {state} within {timeout}s. Last `mngr list`:\n{last['out']}"
        )
    return last["out"]


def _assistant_transcript(env: dict[str, str], cwd: Path, agent: str) -> str:
    """Return the agent's assistant-role transcript text (lower-cased), or "" if none yet.

    A freshly-created agent has no common transcript until its first turn produces
    events, and ``mngr transcript`` exits non-zero in that window -- treated here as
    "nothing yet" so callers can poll.
    """
    result = _run_mngr(env, cwd, "transcript", agent, "--role", "assistant", "--format", "json", timeout=30.0)
    if result.returncode != 0:
        return ""
    return result.stdout.lower()


def _wait_for_assistant_reply(env: dict[str, str], cwd: Path, agent: str, needle: str, timeout: float = 180.0) -> str:
    """Poll the assistant transcript until it contains ``needle`` (lower-cased); return it.

    This is the race-free way to wait for a turn to complete: the agent momentarily
    reports WAITING *before* a turn starts (the marker is only set on
    ``UserPromptSubmit``), so polling ``mngr list`` for WAITING can match the
    pre-turn idle state. Waiting for the actual reply text avoids that entirely.
    """
    last = {"transcript": ""}

    def _has_reply() -> bool:
        last["transcript"] = _assistant_transcript(env, cwd, agent)
        return needle.lower() in last["transcript"]

    if not poll_until(condition=_has_reply, timeout=timeout, poll_interval=3.0):
        raise AssertionError(
            f"Assistant reply containing {needle!r} did not appear within {timeout}s. "
            f"Assistant transcript:\n{last['transcript']}"
        )
    return last["transcript"]


@pytest.mark.timeout(900)
def test_codex_end_to_end_lifecycle_and_resume(tmp_path: Path) -> None:
    """Create a real codex agent, verify the lifecycle/transcript, then resume across restart.

    Asserts the milestone-1-4 behaviors against the real binary: the agent reaches
    WAITING after its turn (lifecycle marker), its reply is captured in the
    transcript, and after ``mngr stop``/``start`` a follow-up question is answered
    using context from before the restart -- proving ``codex resume`` carried the
    conversation forward.
    """
    host_dir = tmp_path / "host"
    repo = tmp_path / "repo"
    user_codex_home = tmp_path / "user_codex"
    host_dir.mkdir()
    user_codex_home.mkdir()
    # A private tmux server (short /tmp path -- tmux socket paths are length-limited)
    # so the agent's sessions never touch the real server this test may run inside.
    tmux_tmpdir = Path(tempfile.mkdtemp(prefix="mngr-codex-e2e-tmux-", dir="/tmp"))

    # Seed an isolated codex home with a copy of the real auth (so the agent
    # authenticates without us reading/writing the user's real ~/.codex).
    shutil.copy2(_REAL_AUTH, user_codex_home / "auth.json")
    (user_codex_home / "auth.json").chmod(0o600)
    if _REAL_MODELS_CACHE.exists():
        # Avoids codex's "model metadata not found" degradation on a fresh home.
        shutil.copy2(_REAL_MODELS_CACHE, user_codex_home / "models_cache.json")

    # A git repo is the agent's source/work dir; configure identity locally so the
    # commit works regardless of the (test-redirected) global git config.
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "codex e2e"], cwd=repo, check=True)
    (repo / "README.md").write_text("codex e2e\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    env = _subprocess_env(host_dir, user_codex_home, tmux_tmpdir)
    try:
        # 1. Create + initial message establishing context.
        create = _run_mngr(
            env,
            repo,
            "create",
            _AGENT,
            "codex",
            "-y",
            "--no-connect",
            "-S",
            f"agent_types.codex.model={_CODEX_MODEL}",
            "--message",
            "Remember that the secret word is banana. Reply with only the word: ok",
            timeout=240.0,
        )
        assert create.returncode == 0, f"create failed: {create.stderr}\n{create.stdout}"

        # 2. Wait for the turn to complete and the reply to land in the (common)
        #    transcript -- this exercises readiness detection, the lifecycle marker
        #    (set on UserPromptSubmit, cleared on Stop), and both transcript layers.
        _wait_for_assistant_reply(env, repo, _AGENT, "ok", timeout=180.0)

        # 3. Stop, then start -> the launch command must resume the prior session.
        stop = _run_mngr(env, repo, "stop", _AGENT, timeout=90.0)
        assert stop.returncode == 0, f"stop failed: {stop.stderr}"
        _wait_for_state(env, repo, _AGENT, "STOPPED", timeout=60.0)

        start = _run_mngr(env, repo, "start", _AGENT, "--no-connect", timeout=150.0)
        assert start.returncode == 0, f"start failed: {start.stderr}\n{start.stdout}"

        # 4. Ask a question answerable only from pre-restart context. `mngr message`
        #    waits for the resumed agent's readiness before sending.
        message = _run_mngr(
            env,
            repo,
            "message",
            _AGENT,
            "-m",
            "What is the secret word I told you earlier? Reply with only that single word.",
            timeout=150.0,
        )
        assert message.returncode == 0, f"message failed: {message.stderr}\n{message.stdout}"

        # 5. Resume carried the conversation forward: the model still knows "banana".
        _wait_for_assistant_reply(env, repo, _AGENT, "banana", timeout=180.0)
    finally:
        _run_mngr(env, repo, "destroy", _AGENT, "--force", timeout=120.0)
        _kill_private_tmux_server(tmux_tmpdir)
