"""Release test: full end-to-end flow for the opencode agent type.

Drives the real ``mngr`` CLI against the real ``opencode`` binary (no mocks) through
the shared agent release lifecycle (create -> WAITING -> message -> transcript ->
stop/start resume -> destroy). Uses OpenCode's free OpenCode-Zen model so no API key
is required. The arc and assertions live in
``imbue.mngr.agents.agent_release_testing``; this file only supplies opencode's
plumbing (free-model seeding, env, source) via an :class:`AgentReleaseProfile`.

Release tests do NOT run in CI. Run manually::

    PYTEST_MAX_DURATION_SECONDS=1200 uv run pytest --no-cov --cov-fail-under=0 \\
        -n 0 -m release \\
        libs/mngr_opencode/imbue/mngr_opencode/test_opencode_agent.py

Requires ``opencode`` on PATH (an outbound network connection to OpenCode Zen is
used for the free model).
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

# A free model on the OpenCode Zen provider that needs no credentials. Pinned so the
# test does not depend on the developer's configured default model.
_FREE_MODEL = "opencode/deepseek-v4-flash-free"


class _OpenCodeReleaseProfile(AgentReleaseProfile):
    agent_type = "opencode"
    common_transcript_subdir = "opencode"
    # opencode reliably surfaces the RUNNING marker; its free-model turn does not call a
    # tool, and it does not report token usage.
    observes_running_marker = True
    forces_tool_call = False
    asserts_usage = False

    def unavailable_reason(self) -> str | None:
        if shutil.which("opencode") is None:
            return "Release test requires the `opencode` binary on PATH."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        # setup_test_mngr_env (autouse) has already redirected $HOME to a tmp dir, so
        # seeding ~/.config/opencode/opencode.json writes into the sandbox; the agent
        # inherits the free model via sync_global_config.
        user_config = Path.home() / ".config" / "opencode" / "opencode.json"
        user_config.parent.mkdir(parents=True, exist_ok=True)
        user_config.write_text(json.dumps({"$schema": "https://opencode.ai/config.json", "model": _FREE_MODEL}))

        env = get_subprocess_test_env(root_name="mngr-opencode-release-test")
        project_config_dir = tmp_path / ".mngr-opencode-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        work_dir = tmp_path / "work"
        init_git_repo(work_dir, initial_commit=True)
        return AgentReleaseContext(env=env, workspace=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return ["--no-ensure-clean", "--source", str(ctx.workspace)]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
# The arc's destroy step preserves transcripts to the local preserved/ dir, which rsyncs
# the transcript directories off the (local) host (the resource guard requires this marker).
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_opencode_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_OpenCodeReleaseProfile(), tmp_path)
