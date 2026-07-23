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
from imbue.mngr.agents.agent_release_testing import _send_expecting_success
from imbue.mngr.agents.agent_release_testing import _wait_for_user_message
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import run_concurrent_message_delivery
from imbue.mngr.agents.agent_release_testing import run_message_delivery_journey
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_git_command
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_claude.plugin import extract_blocking_selector_block

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
    # /clear exercises the relaxed slash-command policy end to end (claude records
    # its effect durably as a session-id change, but the send must not depend on it).
    clear_slash_command = "/clear"
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
    """Drive a real claude agent through the full shared release arc and assert it behaves.

    Runs create -> WAITING -> message -> RUNNING -> transcript -> stop/start resume ->
    destroy -> adopt-from-preserved -> recall against the real ``claude`` binary and a real
    (haiku) model. The load-bearing checks (in ``run_agent_release_lifecycle``) fail unless
    claude genuinely ran: it must reach WAITING, flip the RUNNING marker on a forced bash
    tool call, report token usage, and -- after the agent is destroyed -- a fresh agent in a
    new worktree that adopts the preserved session JSONL must recall the pre-destroy secret,
    proving the native session store resumes and cross-cwd re-filing works. A no-op or broken
    lifecycle (agent never runs, marker never flips, or adoption fails to resume) fails these
    assertions rather than passing silently.
    """
    run_agent_release_lifecycle(_ClaudeReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_message_delivery_journey(tmp_path: Path) -> None:
    """Drive the evidence-confirmed send pipeline through its racey delivery scenarios.

    One real claude agent (haiku) walks idle delivery -> send-while-busy (queued
    input) -> rapid sequential sends -> a long buffer-pasted message -> /clear
    under the relaxed policy. Every ``mngr message`` exit 0 is load-bearing:
    strict confirmation succeeds only once the message's own content appears in
    claude's durable transcript (enqueue or user record), and the exactly-once
    assertions prove the pane-gated Enter retries never duplicate a message.
    """
    run_message_delivery_journey(_ClaudeReleaseProfile(), tmp_path)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_concurrent_message_delivery(tmp_path: Path) -> None:
    """Two claude agents on one tmux server, messaged concurrently.

    Both sends must confirm and each message must land exactly once on its own
    agent -- concurrent sends must never cross-confirm against each other's
    submission evidence (the historical failure mode was exactly this kind of
    cross-talk, via latched tmux wait-for signals on a shared server).
    """
    run_concurrent_message_delivery(_ClaudeReleaseProfile(), tmp_path)


# Timeouts for the model-picker dialog tests. Create can provision a real claude; the send is a
# single relaxed slash command whose post-submit dialog check observes for a few seconds.
_MODEL_PICKER_CREATE_TIMEOUT_SECONDS = 600.0
_MODEL_PICKER_SEND_TIMEOUT_SECONDS = 120.0
_MODEL_PICKER_DESTROY_TIMEOUT_SECONDS = 150.0

# Bare ``/model`` opens Claude Code's interactive model picker: a numbered selector (a rule line
# followed by indented options, one highlighted with ``❯``) that blocks until a choice is made.
# This is the load-bearing blocking dialog the hardening must auto-accept or surface as blocked.
# We use the bare command (not ``/model <name>``, which on current Claude versions switches
# directly with no dialog) because the picker is opened reliably and version-independently. The
# highlighted default is the agent's current model, so accepting it (Enter) is a benign no-op that
# just closes the picker.
_MODEL_PICKER_COMMAND = "/model"

# Bump the post-submit observe window above the 2s default so a real host that renders the picker a
# beat late is still caught -- and, incidentally, exercise the configurable
# post_submit_dialog_observe_seconds knob end to end against a live agent.
_MODEL_PICKER_OBSERVE_SECONDS = 4.0


def _setup_ctx_with_auto_accept_depth(
    profile: _ClaudeReleaseProfile, tmp_path: Path, auto_accept_prompt_depth: int
) -> AgentReleaseContext:
    """Set up a release ctx whose project config enables (or disables) post-submit auto-accept.

    Reuses the profile's standard setup, then appends an ``[agent_types.claude]`` section so the
    created agent's config carries the requested depth and a slightly widened observe window.
    """
    ctx = profile.setup(tmp_path)
    settings_path = Path(ctx.env["MNGR_PROJECT_CONFIG_DIR"]) / "settings.local.toml"
    with settings_path.open("a") as settings_file:
        settings_file.write(
            f"\n[agent_types.claude]\n"
            f"auto_accept_prompt_depth = {auto_accept_prompt_depth}\n"
            f"post_submit_dialog_observe_seconds = {_MODEL_PICKER_OBSERVE_SECONDS}\n"
        )
    return ctx


def _create_model_picker_agent(profile: _ClaudeReleaseProfile, ctx: AgentReleaseContext) -> str:
    """Create a real haiku claude agent from ``ctx`` and return its name."""
    agent_name = f"claude-modelpicker-{get_short_random_string()}"
    create = profile.run_mngr(
        ctx,
        "create",
        agent_name,
        profile.agent_type,
        "--no-connect",
        "--yes",
        *profile.create_extra_args(ctx),
        timeout=_MODEL_PICKER_CREATE_TIMEOUT_SECONDS,
    )
    assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
    return agent_name


def _capture_agent_pane(ctx: AgentReleaseContext, agent_name: str) -> str:
    """Capture the agent's primary tmux window pane as plain text (colors stripped, like mngr)."""
    session = ctx.env["MNGR_PREFIX"] + agent_name
    result = subprocess.run(
        ["tmux", "capture-pane", "-p", "-t", f"={session}:0"], capture_output=True, text=True, check=False
    )
    return result.stdout


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_claude_model_picker_does_not_leave_agent_stuck(tmp_path: Path) -> None:
    """Sending ``/model`` opens a blocking selector, which must not leave the agent stuck.

    Bare ``/model`` opens Claude Code's interactive model picker -- a numbered selector that blocks
    the input until a choice is made, and the real-world manifestation of the bug the dialog
    hardening addresses (a ``/model`` prompt silently blocking the client). Against a real haiku
    agent this asserts the user-facing outcome: the send exits 0, the picker is gone afterward (no
    blocking selector remains in the pane -- ``extract_blocking_selector_block`` returns None), and
    a subsequent normal message is still delivered and processed. If the picker had wedged the
    agent, the follow-up message would never reach the transcript.

    Note on scope: mngr's send-confirmation retry already clears an Enter-dismissable selector like
    the picker, so this exercises the end-to-end "``/model`` does not wedge the agent" outcome
    rather than the post-submit auto-accept path specifically. The auto-accept mechanics and the
    delivered-but-blocked (exit 7) surfacing are covered deterministically by the plugin unit tests
    (see ``plugin_test.py``), which script a selector that persists.
    """
    profile = _ClaudeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = _setup_ctx_with_auto_accept_depth(profile, tmp_path, auto_accept_prompt_depth=5)
    agent_name = _create_model_picker_agent(profile, ctx)
    run_id = get_short_random_string()
    try:
        result = profile.run_mngr(
            ctx, "message", agent_name, "--message", _MODEL_PICKER_COMMAND, timeout=_MODEL_PICKER_SEND_TIMEOUT_SECONDS
        )
        assert result.returncode == 0, (
            f"expected /model to deliver and exit 0, got {result.returncode}:\n{result.stdout}\n{result.stderr}"
        )
        pane = _capture_agent_pane(ctx, agent_name)
        assert extract_blocking_selector_block(pane) is None, (
            f"the /model picker was not dismissed; a blocking selector still remains in the pane:\n{pane}"
        )
        # The agent must still accept and process a normal message -- i.e. it is not wedged on the picker.
        token = f"AFTERMODEL-{run_id}"
        _send_expecting_success(profile, ctx, agent_name, f"Remember this exact value: {token}. Reply with just OK.")
        _wait_for_user_message(
            ctx.host_dir,
            profile.common_transcript_subdir,
            token,
            description="message sent after /model was not processed -- the agent may be stuck on the picker",
        )
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_MODEL_PICKER_DESTROY_TIMEOUT_SECONDS)
