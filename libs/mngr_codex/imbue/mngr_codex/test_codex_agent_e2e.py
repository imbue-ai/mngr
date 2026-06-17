"""Release test: full end-to-end codex agent flow against the real ``codex`` CLI.

Drives the real ``mngr`` CLI through the shared agent release lifecycle (create ->
WAITING -> message -> transcript -> stop/start resume -> destroy). The arc and
assertions live in ``imbue.mngr.agents.agent_release_testing``; this file supplies
codex's plumbing via an :class:`AgentReleaseProfile`.

codex's only real specifics over the other ports: a throwaway ``CODEX_HOME`` seeded
with a copy of the user's real ``~/.codex/auth.json`` (so it authenticates without
touching the real config), and putting the real ``codex`` binary on ``PATH``. ``mngr``
itself is run via ``uv run mngr`` from the checkout (like the sibling release tests), so
local changes are exercised. Host-dir and tmux-server isolation come for free from the
autouse ``setup_test_mngr_env`` fixture, same as every other test.

It is a ``release`` test (not run in CI) and requires the ``codex`` binary plus a
logged-in ``~/.codex/auth.json``; skipped otherwise. The model is pinned to a
ChatGPT-account-safe slug because codex's default ``*-codex`` model is rejected for
ChatGPT-account logins (see the lib README).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_mngr_subprocess

# Resolved at import time, before the autouse ``setup_test_mngr_env`` fixture redirects
# $HOME / mutates PATH: the real home (auth source) and the real codex binary.
_REAL_HOME = Path.home()
_CODEX_BIN = shutil.which("codex") or next(
    (
        candidate
        for candidate in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex", str(_REAL_HOME / ".local/bin/codex"))
        if Path(candidate).exists()
    ),
    None,
)
_REAL_AUTH = _REAL_HOME / ".codex" / "auth.json"
_REAL_MODELS_CACHE = _REAL_HOME / ".codex" / "models_cache.json"

# ChatGPT-account-safe model. codex's compiled default is a ``*-codex`` slug a ChatGPT
# login rejects with a 400. Update if this account's available models change.
_CODEX_MODEL = "gpt-5.5"

# Disable the remote-provider backends for every command: a purely local agent test,
# and leaving them on makes mngr probe Modal/Docker. ``-S`` is a per-command option, so
# it is injected right after the subcommand.
_PROVIDER_SETTINGS: tuple[str, ...] = (
    "-S",
    "providers.modal.is_enabled=false",
    "-S",
    "providers.docker.is_enabled=false",
)


class _CodexReleaseProfile(AgentReleaseProfile):
    agent_type = "codex"
    common_transcript_subdir = "codex"
    # codex forces the bash tool call (run unattended via approval_policy=never, set in
    # create_extra_args; its converter surfaces it as a nested assistant tool_call). It does
    # not report token usage, so that assertion is off (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = False
    # This is the store the adopt-from-preserved arc adopts: after destroy, a fresh agent
    # in a new worktree adopts the just-preserved session by id and must recall the
    # pre-destroy secret -- proving the rollout store resumes and the cwd rebind avoids the modal.
    native_session_preserved_relpaths = ("plugin/codex/home/sessions",)

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # The preserved tree records the root codex session id at ``codex_root_session``
        # (ROOT_SESSION_FILENAME); the plugin resolves that id against the preserved
        # store. Reading the file keeps the test independent of the rollout's date path.
        return (preserved_dir / "codex_root_session").read_text().strip()

    def unavailable_reason(self) -> str | None:
        if _CODEX_BIN is None or not _REAL_AUTH.exists():
            return "codex CLI not installed or ~/.codex/auth.json missing (not logged in)"
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        assert _CODEX_BIN is not None
        # Inherits the autouse fixture's isolated MNGR_HOST_DIR, redirected $HOME, and private
        # tmux server (via copied os.environ); we only add codex's auth home and the real codex bin.
        env = get_subprocess_test_env(root_name="mngr-codex-release-test")

        user_codex_home = tmp_path / "user_codex"
        user_codex_home.mkdir()
        shutil.copy2(_REAL_AUTH, user_codex_home / "auth.json")
        (user_codex_home / "auth.json").chmod(0o600)
        if _REAL_MODELS_CACHE.exists():
            # Avoids codex's "model metadata not found" degradation on a fresh home.
            shutil.copy2(_REAL_MODELS_CACHE, user_codex_home / "models_cache.json")
        env["CODEX_HOME"] = str(user_codex_home)

        # Put the real codex binary on PATH (the autouse fixture redirects HOME).
        # Append, don't prepend: the resource guard prepends its tmux wrapper dir to PATH to
        # track tmux use, and prepending the real bin dir (which also holds the real tmux)
        # would shadow that wrapper and trip the guard's "marked tmux but never invoked" check.
        env["PATH"] = env.get("PATH", "") + os.pathsep + str(Path(_CODEX_BIN).parent)

        repo = tmp_path / "repo"
        init_git_repo(repo)
        return AgentReleaseContext(env=env, workspace=repo, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # Pass the work dir via --source (rather than the mngr cwd) so ``mngr`` can run from
        # the checkout under ``uv run`` -- matching the sibling release tests.
        # auto_allow_permissions sets approval_policy=never, so the forced bash tool call
        # runs without pausing on an approval prompt.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.workspace),
            "-S",
            f"agent_types.codex.model={_CODEX_MODEL}",
            "-S",
            "agent_types.codex.auto_allow_permissions=true",
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        # ``uv run mngr`` from the checkout (the default cwd), so local changes are exercised,
        # with the provider-disabling -S flags injected right after the subcommand.
        return run_mngr_subprocess(args[0], *_PROVIDER_SETTINGS, *args[1:], env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_codex_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_CodexReleaseProfile(), tmp_path)
