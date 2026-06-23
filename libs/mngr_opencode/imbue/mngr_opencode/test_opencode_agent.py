"""Release tests: end-to-end flows for the opencode agent type.

Drive the real ``mngr`` CLI against the real ``opencode`` binary (no mocks). Two tests:

* ``test_opencode_agent_full_lifecycle`` -- the shared agent release lifecycle
  (create -> WAITING -> message -> transcript -> stop/start resume -> destroy).
* ``test_opencode_waiting_reason_reports_permissions`` -- a bash tool call under a
  ``bash: ask`` policy blocks on an approval prompt, and the lifecycle plugin raises
  the ``permissions_waiting`` marker the ``waiting_reason`` field reports as
  ``PERMISSIONS``. This is the only check exercising opencode's real
  ``permission.asked`` event wiring live (the sdk type stubs disagree on the name,
  verified against opencode 1.17.7).

Both use OpenCode's free OpenCode-Zen model so no API key is required. The lifecycle
arc and assertions live in ``imbue.mngr.agents.agent_release_testing``; this file
supplies opencode's plumbing (free-model seeding, env, source) via an
:class:`AgentReleaseProfile`.

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
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME

# A free model on the OpenCode Zen provider that needs no credentials. Pinned so the
# test does not depend on the developer's configured default model.
_FREE_MODEL = "opencode/deepseek-v4-flash-free"

# Mirror the harness's private timeouts (kept local so this file does not import them).
_CREATE_TIMEOUT_SECONDS = 600.0
_MESSAGE_TIMEOUT_SECONDS = 180.0
_LIFECYCLE_TIMEOUT_SECONDS = 150.0
# A prompt that reliably drives even the free model to call the bash tool (verified
# live against opencode 1.17.7); under a `bash: ask` policy this blocks on approval.
_BASH_TRIGGER_PROMPT = (
    "Use the bash tool to run exactly this shell command and show me its output: echo hello-from-opencode"
)
# How long to wait for the blocking approval prompt to surface the marker (it appeared
# in ~3s live; generous here to absorb model/launch latency).
_PERMISSION_MARKER_TIMEOUT_SECONDS = 120.0


class _OpenCodeReleaseProfile(AgentReleaseProfile):
    agent_type = "opencode"
    common_transcript_subdir = "opencode"
    # opencode's free model forces the bash tool call (run unattended via
    # auto_allow_permissions, set in create_extra_args). It does not report token usage, so
    # that assertion is off (observing the RUNNING marker is universal).
    forces_tool_call = True
    asserts_usage = False
    # Only the SQLite db is reliably present after a short turn; the WAL sidecars and
    # the storage/ dir are conditional (created only when there are uncheckpointed writes
    # / on-disk message parts), so the plugin preserves them when present but the arc does
    # not require them.
    # This is the store the adopt-from-preserved arc adopts: after destroy, the arc adopts
    # the preserved session into a fresh agent (new worktree) and asserts it recalls the
    # pre-destroy secret -- proving the preserved db resumes once the plugin copies it in and
    # rebinds the stored source-worktree path to the new work dir.
    native_session_preserved_relpaths = ("plugin/opencode/data/opencode/opencode.db",)

    def adopt_session_arg(self, preserved_dir: Path) -> str:
        # The launch script records the root session id here; the plugin resolves this id
        # against the preserved store's opencode.db (and rebinds it onto the new work dir).
        return (preserved_dir / ROOT_SESSION_FILENAME).read_text().strip()

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
        # auto_allow_permissions injects a wildcard permission allow so the forced bash
        # tool call runs without pausing on an approval prompt.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.workspace),
            "-S",
            "agent_types.opencode.auto_allow_permissions=true",
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1500)
def test_opencode_agent_full_lifecycle(tmp_path: Path) -> None:
    run_agent_release_lifecycle(_OpenCodeReleaseProfile(), tmp_path)


class _OpenCodePermissionReleaseProfile(_OpenCodeReleaseProfile):
    """Base profile, but the per-agent config requires approval for ``bash``.

    A bash tool call then blocks on an approval prompt instead of running, which is
    what drives ``waiting_reason`` to ``PERMISSIONS``.
    """

    def setup(self, tmp_path: Path) -> AgentReleaseContext:
        ctx = super().setup(tmp_path)
        # setup_test_mngr_env (autouse) keeps $HOME redirected for the whole test, so
        # re-seeding the user config writes into the sandbox; the agent inherits the
        # `bash: ask` policy via sync_global_config.
        user_config = Path.home() / ".config" / "opencode" / "opencode.json"
        user_config.write_text(
            json.dumps(
                {
                    "$schema": "https://opencode.ai/config.json",
                    "model": _FREE_MODEL,
                    "permission": {"bash": "ask"},
                }
            )
        )
        return ctx


def _opencode_agent_state_dir(host_dir: Path) -> Path:
    candidates = [path for path in (host_dir / "agents").glob("*") if path.is_dir()]
    assert len(candidates) == 1, f"expected exactly one agent state dir under {host_dir}/agents, found {candidates}"
    return candidates[0]


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(900)
def test_opencode_waiting_reason_reports_permissions(tmp_path: Path) -> None:
    """End-to-end: a blocking approval prompt raises the ``permissions_waiting`` marker.

    Under a ``bash: ask`` policy a bash tool call blocks on an approval prompt; the
    lifecycle plugin raises ``permissions_waiting`` on opencode's real
    ``permission.asked`` event, which is what the ``waiting_reason`` field generator
    reports as ``PERMISSIONS`` (and what promotes the agent RUNNING -> WAITING). This is
    the only check that exercises that event wiring against the live opencode binary --
    the sdk type stubs disagree with the binary on the event name, so a unit test alone
    would not catch a regression to the real contract. The idle-side ``END_OF_TURN``
    (marker absent) is asserted right after create.
    """
    profile = _OpenCodePermissionReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"opencode-perm-{get_short_random_string()}"
    try:
        create = profile.run_mngr(
            ctx,
            "create",
            agent_name,
            profile.agent_type,
            "--no-connect",
            "--yes",
            *profile.create_extra_args(ctx),
            timeout=_CREATE_TIMEOUT_SECONDS,
        )
        assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"

        state_dir = _opencode_agent_state_dir(ctx.host_dir)
        permissions_marker = state_dir / PERMISSIONS_WAITING_FILENAME
        active_marker = state_dir / ACTIVE_MARKER_FILENAME

        # Idle right after create: no pending prompt and no active turn -- the state the
        # field generator maps to END_OF_TURN.
        assert not permissions_marker.exists(), "no prompt should be pending right after create"
        assert not active_marker.exists(), "expected WAITING (no active marker) right after create"

        # Trigger a bash tool call; under `bash: ask` it blocks on an approval prompt.
        # send is fire-and-forget (prompt_async), so this returns without waiting on the
        # turn -- which never completes while blocked.
        message = profile.run_mngr(
            ctx, "message", agent_name, "--message", _BASH_TRIGGER_PROMPT, timeout=_MESSAGE_TIMEOUT_SECONDS
        )
        assert message.returncode == 0, f"message failed:\n{message.stdout}\n{message.stderr}"

        assert poll_until(
            condition=permissions_marker.exists, timeout=_PERMISSION_MARKER_TIMEOUT_SECONDS, poll_interval=0.5
        ), "permissions_waiting never appeared -> opencode never reported a blocking approval prompt"
        # The session stays busy (active present) while a prompt is open, so the base
        # state is RUNNING and get_lifecycle_state promotes it to WAITING.
        assert active_marker.exists(), "expected the active marker present while blocked on an approval prompt"
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        if ctx.teardown is not None:
            ctx.teardown()
