"""Release test: full end-to-end lifecycle of a real mngr-managed pi agent.

Drives the real ``mngr`` CLI against the real ``pi`` binary and a real model through
the shared agent release lifecycle (create -> WAITING -> message -> RUNNING ->
transcript -> stop/start resume -> destroy). The arc and assertions live in
``imbue.mngr.agents.agent_release_testing``; this file supplies pi's plumbing via an
:class:`AgentReleaseProfile`.

pi exercises the richest end of the shared lifecycle: it observes the RUNNING marker,
forces a bash tool call (so the transcript carries a tool_result), and reports token
usage -- the capability flags below turn those shared assertions on.

The git source includes a ``.agents/skills`` dir, which would trip pi 0.79+'s "Trust
project folder?" dialog; ``mngr create --yes`` makes the plugin pre-seed trust, so a
regression there would stall the first message and fail the lifecycle assertions.

Requires ``pi`` on PATH and ``ANTHROPIC_API_KEY`` in the environment; skipped otherwise.
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
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess

# A fast, cheap, tool-capable model keeps the real turns short.
_MODEL = "claude-haiku-4-5"


class _PiReleaseProfile(AgentReleaseProfile):
    agent_type = "pi-coding"
    common_transcript_subdir = "pi-coding"
    # pi forces the bash tool call and reports token usage, so both gated assertions apply
    # (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = True
    # This is the store the adopt-from-preserved arc adopts (see adopt_session_arg).
    native_session_preserved_relpaths = ("plugin/pi_coding/sessions",)

    def unavailable_reason(self) -> str | None:
        if shutil.which("pi") is None or not os.environ.get("ANTHROPIC_API_KEY"):
            return "Release test requires ANTHROPIC_API_KEY in the environment and `pi` on PATH."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        env = get_subprocess_test_env(root_name="mngr-pi-release-test")
        project_config_dir = tmp_path / ".mngr-pi-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        # A git source with a .agents/skills dir gives the worktree pi "project trust
        # inputs" (pi 0.79+ would otherwise stall at its trust dialog).
        work_dir = tmp_path / "pi-source"
        init_git_repo(work_dir, initial_commit=True)
        (work_dir / ".gitignore").write_text(".pi/\n")
        skills_dir = work_dir / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "example.md").write_text("# Example skill (gives the worktree pi trust inputs)\n")
        run_git_command(work_dir, "add", ".gitignore", ".agents")
        run_git_command(work_dir, "commit", "-m", "add gitignore and .agents/skills")

        return AgentReleaseContext(env=env, workspace=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.workspace),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--provider",
            "anthropic",
            "--model",
            _MODEL,
        ]

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        """Return the absolute path to the preserved pi session JSONL to adopt.

        ``pi_session_file`` (preserved alongside the store) holds the live agent's
        session path; only its basename is stable across the destroy, so locate the
        matching JSONL under the preserved ``sessions`` store and return that path.
        """
        pointer = preserved_dir / "pi_session_file"
        session_basename = Path(pointer.read_text().strip()).name
        sessions_root = preserved_dir / "plugin" / "pi_coding" / "sessions"
        matches = [path for path in sessions_root.glob(f"*/{session_basename}")]
        assert len(matches) == 1, (
            f"expected exactly one preserved pi session named {session_basename} under {sessions_root}, "
            f"found {matches}"
        )
        return str(matches[0])

    def prepare_adoption_workspace(self, work_dir: Path) -> None:
        """Seed the fresh adoption worktree with the same trust inputs the base source has.

        A ``.agents/skills`` dir gives the worktree pi "project trust inputs"; ``mngr
        create --yes`` pre-seeds trust so the dialog never appears, matching ``setup``.
        """
        init_git_repo(work_dir, initial_commit=True)
        (work_dir / ".gitignore").write_text(".pi/\n")
        skills_dir = work_dir / ".agents" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "example.md").write_text("# Example skill (gives the worktree pi trust inputs)\n")
        run_git_command(work_dir, "add", ".gitignore", ".agents")
        run_git_command(work_dir, "commit", "-m", "add gitignore and .agents/skills")

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_pi_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_PiReleaseProfile(), tmp_path)
