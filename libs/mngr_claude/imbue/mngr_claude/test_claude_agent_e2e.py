"""Release test: full end-to-end lifecycle of a real mngr-managed claude agent.

Drives the real ``mngr`` CLI against the real ``claude`` binary and a real model
through the shared agent release lifecycle (create -> WAITING -> message -> RUNNING
-> transcript -> stop/start resume -> destroy -> adopt-from-preserved -> recall). The
arc and assertions live in ``imbue.mngr.agents.agent_release_testing``; this file
supplies claude's plumbing via an :class:`AgentReleaseProfile`.

claude runs the same shared arc as every other port: it observes the RUNNING marker (its
UserPromptSubmit hook touches the ``active`` marker), forces a bash tool call, and -- with
``asserts_usage`` on -- reports token usage. Its plumbing differs from the sibling ports
only in:

* Repo-local ``.gitignore``. claude's preflight refuses to write hooks to
  ``.claude/settings.local.json`` unless the repository's *own* ``.gitignore``
  excludes it (a global rule is rejected, since remote hosts lack it).
  ``_init_claude_workspace`` seeds that rule for both the seed worktree and the fresh
  adoption worktree; the sibling ports don't need this.

* Custom-API-key approval. The plugin's ``approve_api_key_for_claude`` pre-approves the
  passed-in ``ANTHROPIC_API_KEY`` during provision, so claude doesn't block on its
  custom-key dialog (no sibling port has one). claude's other first-run dialogs
  (onboarding/effort) and work-dir trust are dismissed by the ``--yes`` the harness
  already passes for every agent -- not a claude specific -- so the test seeds no config.

* Post-``--`` args. ``--dangerously-skip-permissions`` lets the forced bash tool call
  run without a permission pause, ``--pass-env ANTHROPIC_API_KEY`` carries the key to the
  agent, and ``--model haiku`` pins the cheapest tier (the seed/recall turns don't need
  more).

* Adoption resolves by the preserved session JSONL's absolute path. claude has no
  root-session-id sidecar file (unlike codex); the preserved native store is the
  per-agent ``projects/<encoded-work-dir>/<session-id>.jsonl`` tree, and
  ``_resolve_adopt_session`` accepts a ``.jsonl`` path directly, so the path is both
  unambiguous and independent of the encoded-cwd subdir name.

Requires ``claude`` on PATH and ``ANTHROPIC_API_KEY`` in the environment; skipped
otherwise. Release-marked, so it does not run in CI.
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

# claude's native resumable session store, relative to the agent state dir: the
# per-agent Claude config dir's session JSONLs (see ``_AGENT_CLAUDE_PROJECTS_RELPATH``
# / ``_claude_preserved_items`` in plugin.py). preserve_sessions_on_destroy copies
# this tree to preserved/, and adopt_session_arg resolves the JSONL out of it.
_CLAUDE_PROJECTS_RELPATH = "plugin/claude/anthropic/projects"

# Pin the cheapest tier: the seed/recall turns just plant and echo a secret, so a frontier
# model would only add cost and latency to the release run. ``haiku`` is Claude Code's alias
# for the current Haiku.
_MODEL = "haiku"


def _init_claude_workspace(path: Path) -> None:
    """Init a git repo whose own .gitignore excludes Claude's settings.local.json.

    mngr's claude preflight refuses to write hooks to .claude/settings.local.json
    unless the repository's *own* .gitignore excludes it (a global rule is rejected,
    since remote hosts lack it). Both the seed worktree and the fresh adoption
    worktree must carry that rule, so this replaces the bare init_git_repo for each.
    """
    init_git_repo(path, initial_commit=False)
    (path / ".gitignore").write_text(".claude/settings.local.json\n")
    run_git_command(path, "add", ".gitignore")
    run_git_command(path, "commit", "-m", "Add .gitignore")


class _ClaudeReleaseProfile(AgentReleaseProfile):
    agent_type = "claude"
    common_transcript_subdir = "claude"
    # claude's forced seed turn runs a bash tool call and its converter emits per-message
    # token usage, so both gated assertions apply (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = True
    # This is the store the adopt-from-preserved arc adopts: after destroy, a fresh agent
    # in a new worktree adopts the just-preserved session and must recall the pre-destroy
    # secret -- proving the store resumes and the cross-cwd re-filing works.
    native_session_preserved_relpaths = (_CLAUDE_PROJECTS_RELPATH,)

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # Return the absolute path of the single preserved session JSONL. The shallow
        # ``*/*.jsonl`` glob targets ``projects/<encoded-work-dir>/<session-id>.jsonl``
        # and excludes nested subagent transcripts at ``<sid>/subagents/*.jsonl``.
        # Passing the path (not a bare session id) keeps adoption unambiguous: the
        # resolver otherwise searches every live and preserved agent's projects/ dir.
        projects_root = preserved_dir / _CLAUDE_PROJECTS_RELPATH
        matches = list(projects_root.glob("*/*.jsonl"))
        assert len(matches) == 1, (
            f"expected exactly one preserved claude session JSONL under {projects_root}, found {matches}"
        )
        return str(matches[0])

    def unavailable_reason(self) -> str | None:
        if shutil.which("claude") is None or not os.environ.get("ANTHROPIC_API_KEY"):
            return "Release test requires ANTHROPIC_API_KEY in the environment and `claude` on PATH."
        return None

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        # ``mngr create --yes`` dismisses claude's first-run dialogs and trusts the work dir,
        # and the plugin's ``approve_api_key_for_claude`` pre-approves the key, so no seeded
        # ~/.claude.json is needed. The env carries the redirected HOME and the isolated
        # MNGR_HOST_DIR / tmux server from the autouse fixture.
        env = get_subprocess_test_env(root_name="mngr-claude-release-test")

        # Disable the remote providers for every command: a purely local agent test, and
        # leaving them on makes mngr probe Modal/Docker (and rejects the autouse test prefix).
        project_config_dir = tmp_path / ".mngr-claude-test"
        project_config_dir.mkdir(parents=True, exist_ok=True)
        (project_config_dir / "settings.local.toml").write_text(
            "is_allowed_in_pytest = true\n\n[providers.modal]\nis_enabled = false\n\n[providers.docker]\nis_enabled = false\n"
        )
        env["MNGR_PROJECT_CONFIG_DIR"] = str(project_config_dir)

        work_dir = tmp_path / "claude-source"
        _init_claude_workspace(work_dir)
        return AgentReleaseContext(env=env, workspace=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def prepare_adoption_workspace(self, work_dir: Path) -> None:
        # The adoption worktree is also a claude source, so it needs the same
        # repo-local .gitignore rule the seed worktree carries (see _init_claude_workspace).
        _init_claude_workspace(work_dir)

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # Pass the work dir via --source (so mngr runs from the checkout under ``uv run``)
        # and the API key into the agent. ``--dangerously-skip-permissions`` lets the
        # forced bash tool call run without pausing on a permission dialog.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.workspace),
            "--pass-env",
            "ANTHROPIC_API_KEY",
            "--",
            "--dangerously-skip-permissions",
            "--model",
            _MODEL,
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_ClaudeReleaseProfile(), tmp_path)
