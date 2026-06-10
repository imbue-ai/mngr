"""Release test: full end-to-end flow for the antigravity (``agy``) agent type.

Drives the real ``mngr`` CLI against the real ``agy`` binary through the shared agent
release lifecycle (create -> WAITING -> message -> transcript -> stop/start resume ->
destroy). The arc and assertions live in ``imbue.mngr.agents.agent_release_testing``;
this file supplies antigravity's plumbing via an :class:`AgentReleaseProfile`.

antigravity authenticates from ``$HOME/.gemini`` (it has no config-dir override -- the
plugin relocates ``$HOME`` per agent and shares the oauth token by symlink). The autouse
``setup_test_mngr_env`` fixture redirects ``$HOME`` to a temp dir, so this test seeds the
real shared oauth token and a ``settings.json`` into that redirected home for the plugin
to find. Everything else agy needs -- HOME relocation, the non-dotted workspace symlink,
trust seeding, NUX skip -- the plugin handles itself.

The model is pinned to a Claude model via the seeded ``settings.json`` (agy reads its
model from settings, so the spaces/parens in the display name never reach a shell): agy's
default Gemini model can hit per-account usage limits, whereas a Claude model is reliable.

Requires the ``agy`` binary on PATH and a logged-in ``~/.gemini`` (the shared
``antigravity-oauth-token``); skipped otherwise. Not run in CI.
"""

from __future__ import annotations

import json
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
from imbue.mngr_antigravity.antigravity_config import get_antigravity_oauth_token_path

# Resolved at import time, before the autouse fixture redirects $HOME: the real ~/.gemini
# auth the plugin reads and shares into each per-agent home. agy signs in from the
# top-level oauth creds (``oauth_creds.json``/``google_accounts.json``), not just the
# antigravity-cli token -- all of them must be seeded into the redirected HOME or the
# agent comes up unauthenticated and can't run a turn.
_REAL_GEMINI = Path.home() / ".gemini"
_REAL_OAUTH_TOKEN = get_antigravity_oauth_token_path(Path.home())
_REAL_SETTINGS = _REAL_OAUTH_TOKEN.parent / "settings.json"
_TOP_LEVEL_AUTH_FILES = ("oauth_creds.json", "google_accounts.json")

# An agy "models" display name for a Claude model. agy's default Gemini model can hit
# per-account usage limits; a Claude model is reliable. Seeded into settings.json, so the
# spaces/parens never reach a shell (unlike a model passed as a cli_arg). Update if the
# account's available models change.
_MODEL = "Claude Sonnet 4.6 (Thinking)"


class _AntigravityReleaseProfile(AgentReleaseProfile):
    agent_type = "antigravity"
    common_transcript_subdir = "antigravity"
    # agy sets its marker on PreInvocation (turn start) and clears it on Stop, and its
    # send is tmux paste+Enter with no marker-confirm, so polling RUNNING mid-turn is racy;
    # rely on the transcript-keyed assertions. agy's turn forces no tool and reports no
    # token usage in the common envelope.
    observes_running_marker = False
    forces_tool_call = False
    asserts_usage = False

    def unavailable_reason(self) -> str | None:
        if shutil.which("agy") is None or not (_REAL_GEMINI / "oauth_creds.json").exists():
            return "Release test requires the `agy` binary on PATH and a logged-in ~/.gemini (oauth_creds.json)."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-antigravity-release-test")
        project_config_dir = tmp_path / ".mngr-antigravity-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        # Seed the real ~/.gemini auth + a settings.json (pinning a Claude model) into the
        # autouse-redirected $HOME, where the plugin reads them to authenticate and share:
        # the top-level oauth creds (so agy is actually signed in), the antigravity-cli
        # token, and settings.
        seeded_token = get_antigravity_oauth_token_path(Path(env["HOME"]))
        seeded_token.parent.mkdir(parents=True, exist_ok=True)
        seeded_gemini = Path(env["HOME"]) / ".gemini"
        for name in _TOP_LEVEL_AUTH_FILES:
            source = _REAL_GEMINI / name
            if source.exists():
                shutil.copy2(source, seeded_gemini / name)
        shutil.copy2(_REAL_OAUTH_TOKEN, seeded_token)
        settings = json.loads(_REAL_SETTINGS.read_text()) if _REAL_SETTINGS.exists() else {}
        settings["model"] = _MODEL
        (seeded_token.parent / "settings.json").write_text(json.dumps(settings))

        work_dir = tmp_path / "work"
        init_git_repo(work_dir, initial_commit=True)
        return AgentReleaseContext(env=env, workspace=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return ["--no-ensure-clean", "--source", str(ctx.workspace)]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.timeout(1500)
def test_antigravity_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_AntigravityReleaseProfile(), tmp_path)
