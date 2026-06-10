"""Release test: full end-to-end codex agent flow against the real ``codex`` CLI.

Drives the real ``mngr`` CLI through the shared agent release lifecycle (create ->
WAITING -> message -> transcript -> stop/start resume -> destroy). The arc and
assertions live in ``imbue.mngr.utils.agent_release_testing``; this file supplies
codex's plumbing via an :class:`AgentReleaseProfile`.

codex's plumbing is the most involved: an isolated ``MNGR_HOST_DIR`` and a throwaway
``CODEX_HOME`` seeded with a copy of the user's real ``~/.codex/auth.json`` (so it
authenticates without touching the real config), plus a private tmux server so the
agent's sessions never touch the real one. codex's marker is racy mid-turn (set on
``UserPromptSubmit``, cleared on ``Stop``), so the profile opts out of the RUNNING
observation; the transcript-keyed assertions carry the lifecycle coverage.

It is a ``release`` test (not run in CI) and requires the ``codex`` binary plus a
logged-in ``~/.codex/auth.json``; skipped otherwise. The model is pinned to a
ChatGPT-account-safe slug because codex's default ``*-codex`` model is rejected for
ChatGPT-account logins (see the lib README).

Known pre-existing issue (not a harness regression): the post-restart message send
currently times out against the real binary -- codex's *resumed* TUI does not echo the
tmux paste within mngr's send timeout ("Timeout waiting for pasted content to appear").
This reproduces identically on the pre-unification version of this test, so it is a
codex resume-send issue to be fixed separately; the shared harness drives every prior
lifecycle step (create -> WAITING -> message -> transcript conformance -> stop -> start)
correctly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.utils.agent_release_testing import AgentReleaseContext
from imbue.mngr.utils.agent_release_testing import AgentReleaseProfile
from imbue.mngr.utils.agent_release_testing import run_agent_release_lifecycle

# Resolved at import time, before the autouse ``setup_test_mngr_env`` fixture redirects
# $HOME / mutates PATH: the real home (auth source) and the real codex / mngr binaries.
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

# ChatGPT-account-safe model. codex's compiled default is a ``*-codex`` slug a ChatGPT
# login rejects with a 400. Update if this account's available models change.
_CODEX_MODEL = "gpt-5.5"

_TMUX_TMPDIR_PREFIX = "/tmp/mngr-codex-e2e-tmux-"

# Disable the remote-provider backends for every command: a purely local agent test,
# and leaving them on makes mngr probe Modal/Docker. ``-S`` is a per-command option, so
# it is injected right after the subcommand.
_PROVIDER_SETTINGS: tuple[str, ...] = (
    "-S",
    "providers.modal.is_enabled=false",
    "-S",
    "providers.docker.is_enabled=false",
)


def _kill_private_tmux_server(tmux_tmpdir: Path) -> None:
    """Kill the throwaway tmux server for ``tmux_tmpdir`` (guarded against the real server)."""
    tmpdir_str = str(tmux_tmpdir)
    # Safety: only ever kill a server whose socket lives under our private prefix, so a
    # mis-set path can never take down the user's real tmux server.
    assert tmpdir_str.startswith(_TMUX_TMPDIR_PREFIX), (
        f"refusing to kill-server for unexpected TMUX_TMPDIR {tmpdir_str}"
    )
    socket_path = tmux_tmpdir / f"tmux-{os.getuid()}" / "default"
    kill_env = os.environ.copy()
    kill_env.pop("TMUX", None)
    kill_env["TMUX_TMPDIR"] = tmpdir_str
    subprocess.run(["tmux", "-S", str(socket_path), "kill-server"], capture_output=True, env=kill_env)
    shutil.rmtree(tmux_tmpdir, ignore_errors=True)


class _CodexReleaseProfile(AgentReleaseProfile):
    agent_type = "codex"
    common_transcript_subdir = "codex"
    # codex sets its marker only on UserPromptSubmit and clears it on Stop, so polling it
    # mid-turn is racy; rely on the transcript-keyed assertions instead. Its turn does not
    # force a tool call and it does not report usage in the common envelope.
    observes_running_marker = False
    forces_tool_call = False
    asserts_usage = False

    def unavailable_reason(self) -> str | None:
        if _CODEX_BIN is None or _MNGR_BIN is None or not _REAL_AUTH.exists():
            return "codex CLI not installed, mngr not on PATH, or ~/.codex/auth.json missing (not logged in)"
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        assert _CODEX_BIN is not None and _MNGR_BIN is not None
        host_dir = tmp_path / "host"
        repo = tmp_path / "repo"
        user_codex_home = tmp_path / "user_codex"
        host_dir.mkdir()
        user_codex_home.mkdir()
        # Private tmux server (short /tmp path -- tmux sockets are length-limited) so the
        # agent's sessions never touch the real server this test may run inside.
        # Derive the mkdtemp args from _TMUX_TMPDIR_PREFIX so the created path is guaranteed
        # to satisfy the safety guard in _kill_private_tmux_server (the two cannot drift).
        tmux_tmpdir = Path(
            tempfile.mkdtemp(prefix=os.path.basename(_TMUX_TMPDIR_PREFIX), dir=os.path.dirname(_TMUX_TMPDIR_PREFIX))
        )

        shutil.copy2(_REAL_AUTH, user_codex_home / "auth.json")
        (user_codex_home / "auth.json").chmod(0o600)
        if _REAL_MODELS_CACHE.exists():
            # Avoids codex's "model metadata not found" degradation on a fresh home.
            shutil.copy2(_REAL_MODELS_CACHE, user_codex_home / "models_cache.json")

        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)
        subprocess.run(["git", "config", "user.name", "codex e2e"], cwd=repo, check=True)
        (repo / "README.md").write_text("codex e2e\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

        env = os.environ.copy()
        env["MNGR_HOST_DIR"] = str(host_dir)
        env["CODEX_HOME"] = str(user_codex_home)
        env.pop("TMUX", None)
        env["TMUX_TMPDIR"] = str(tmux_tmpdir)
        extra_path = os.pathsep.join({str(Path(_CODEX_BIN).parent), str(Path(_MNGR_BIN).parent)})
        env["PATH"] = extra_path + os.pathsep + env.get("PATH", "")

        return AgentReleaseContext(
            env=env,
            workspace=repo,
            host_dir=host_dir,
            teardown=lambda: _kill_private_tmux_server(tmux_tmpdir),
        )

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # No --source: codex takes its source/work dir from the mngr cwd (the repo).
        return ["-S", f"agent_types.codex.model={_CODEX_MODEL}"]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        assert _MNGR_BIN is not None
        # The real mngr binary (resolved before the HOME redirect), run from the repo, with
        # the provider-disabling -S flags injected right after the subcommand.
        return subprocess.run(
            [_MNGR_BIN, args[0], *_PROVIDER_SETTINGS, *args[1:]],
            env=dict(ctx.env),
            cwd=str(ctx.workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )


@pytest.mark.release
# Marked tmux because teardown invokes ``tmux kill-server`` in-process to tear down the
# agent's private tmux server (the resource guard requires the marker for in-process tmux
# use). The server is a throwaway under /tmp/mngr-codex-e2e-tmux-*, never the real one.
@pytest.mark.tmux
@pytest.mark.timeout(900)
def test_codex_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_CodexReleaseProfile(), tmp_path)
