"""Release tests: end-to-end flows for the opencode agent type.

Drive the real ``mngr`` CLI against the real ``opencode`` binary (no mocks):

* ``test_opencode_agent_full_lifecycle`` -- the shared agent release lifecycle
  (create -> WAITING -> message -> transcript -> stop/start resume -> destroy).
* ``test_opencode_waiting_reason_reports_permissions`` -- a bash tool call under a
  ``bash: ask`` policy blocks on an approval prompt, and the lifecycle plugin raises
  the ``permissions_waiting`` marker the ``waiting_reason`` field reports as
  ``PERMISSIONS``. This is the only check exercising opencode's real
  ``permission.asked`` event wiring live (the sdk type stubs disagree on the name,
  verified against opencode 1.17.7).
* ``test_opencode_turn_end_reports_a_completed_and_an_interrupted_turn`` and
  ``test_opencode_turn_end_reports_a_declined_permission_as_interrupted`` -- ``mngr list``
  publishes the ``turn_end`` the lifecycle plugin writes from opencode's own events.
* ``test_opencode_upgrade_restarts_an_idle_agent_on_its_packaged_plugin_and_keeps_its_conversation``
  -- ``mngr upgrade`` swaps a stale lifecycle plugin back in, the restarted agent resumes its
  session, and the upgraded plugin records the next turn.

All use OpenCode's free OpenCode-Zen model so no API key is required. The lifecycle
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
import urllib.request
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pytest

from imbue.mngr.agents.agent_release_testing import AgentReleaseContext
from imbue.mngr.agents.agent_release_testing import AgentReleaseProfile
from imbue.mngr.agents.agent_release_testing import run_agent_release_lifecycle
from imbue.mngr.agents.agent_release_testing import send_expecting_success
from imbue.mngr.hosts.background_tasks import TURN_END_FILE_NAME
from imbue.mngr.utils.polling import poll_until
from imbue.mngr.utils.testing import get_short_random_string
from imbue.mngr.utils.testing import get_subprocess_test_env
from imbue.mngr.utils.testing import init_git_repo
from imbue.mngr.utils.testing import read_turn_end_in
from imbue.mngr.utils.testing import run_mngr_subprocess
from imbue.mngr_opencode.opencode_config import ACTIVE_MARKER_FILENAME
from imbue.mngr_opencode.opencode_config import COMMON_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import PERMISSIONS_WAITING_FILENAME
from imbue.mngr_opencode.opencode_config import RAW_TRANSCRIPT_RELATIVE_PATH
from imbue.mngr_opencode.opencode_config import ROOT_SESSION_FILENAME
from imbue.mngr_opencode.opencode_config import SERVER_PORT_FILENAME
from imbue.mngr_opencode.opencode_config import get_opencode_config_dir
from imbue.mngr_opencode.opencode_config import get_opencode_plugin_path

# A free model on the OpenCode Zen provider that needs no credentials. Pinned so the
# test does not depend on the developer's configured default model.
_FREE_MODEL = "opencode/deepseek-v4.1-flash-free"

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
_TURN_TIMEOUT_SECONDS = 180.0
_SHORT_REPLY_PROMPT = "Reply with exactly the word: done"
_LONG_BASH_PROMPT = "Use the bash tool to run exactly this shell command and show me its output: sleep 300"
_TURN_END_CONSUMER = (
    "mngr_opencode_plugin.ts's writeTurnEnd, which reads the root session's `session.status` busy and idle, "
    "`session.error`, `permission.replied` and `question.rejected`, and its reply's `finish`, `error` and tool parts"
)
_UPGRADE_TIMEOUT_SECONDS: Final[float] = 300.0
# opencode installs its plugin dependencies on its first run in a fresh home, which each release test has.
_FIRST_RUN_READY_TIMEOUT_SECONDS: Final[int] = 120
_RECALL_PROMPT: Final[str] = "What is the secret I asked you to remember? Do not use any tools. Reply with just it."


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
        return AgentReleaseContext(env=env, project_dir=work_dir, host_dir=Path(env["MNGR_HOST_DIR"]))

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # auto_allow_permissions injects a wildcard permission allow so the forced bash
        # tool call runs without pausing on an approval prompt.
        return [
            "--no-ensure-clean",
            "--source",
            str(ctx.project_dir),
            "-S",
            "agent_types.opencode.auto_allow_permissions=true",
        ]

    def run_mngr(self, ctx: AgentReleaseContext, *args: str, timeout: float) -> subprocess.CompletedProcess[str]:
        return run_mngr_subprocess(*args, env=dict(ctx.env), timeout=timeout)


@pytest.mark.release
@pytest.mark.tmux
@pytest.mark.rsync
# Known-flaky: create intermittently fails with "OpenCode did not signal readiness within 10s". Loading
# mngr's opencode plugins makes the first session take 4-6s to create, leaving little of that budget.
@pytest.mark.flaky
@pytest.mark.timeout(1500)
def test_opencode_agent_full_lifecycle(tmp_path: Path) -> None:
    """End-to-end: the opencode agent survives the full shared agent release lifecycle.

    Drives the real ``mngr`` CLI against the real ``opencode`` binary (free OpenCode-Zen
    model, no API key) through create -> WAITING -> message -> transcript -> stop/start
    resume -> destroy -> adopt-from-preserved, via the shared
    ``run_agent_release_lifecycle`` arc parameterized by ``_OpenCodeReleaseProfile``. The
    arc's assertions confirm opencode actually performs each step: the agent reaches
    WAITING, a forced bash tool call produces transcript output, a stop/start cycle
    resumes the same session, and after destroy the preserved ``opencode.db`` is adopted
    into a fresh worktree where the agent recalls a pre-destroy secret -- so the test
    fails (not no-ops) if seeding, transcript capture, resume, or session preservation
    breaks. Token-usage assertion is off because opencode's free model reports none.
    """
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

    def create_extra_args(self, ctx: AgentReleaseContext) -> Sequence[str]:
        # Without the base profile's auto_allow_permissions: its wildcard allow replaces the
        # whole permission block, so the `bash: ask` policy above would never prompt.
        return ["--no-ensure-clean", "--source", str(ctx.project_dir)]


def _opencode_agent_state_dir(host_dir: Path) -> Path:
    candidates = [path for path in (host_dir / "agents").glob("*") if path.is_dir()]
    assert len(candidates) == 1, f"expected exactly one agent state dir under {host_dir}/agents, found {candidates}"
    return candidates[0]


@pytest.mark.release
@pytest.mark.harness_internals(harness="opencode")
@pytest.mark.tmux
@pytest.mark.rsync
# Known-flaky: the same create-readiness timeout as test_opencode_agent_full_lifecycle.
@pytest.mark.flaky
@pytest.mark.timeout(900)
def test_opencode_waiting_reason_reports_permissions(tmp_path: Path) -> None:
    """End-to-end: a blocking approval prompt raises the ``permissions_waiting`` marker.

    Under a ``bash: ask`` policy a bash tool call blocks on an approval prompt; the
    lifecycle plugin raises ``permissions_waiting`` on opencode's real
    ``permission.asked`` event, which is what the ``waiting_reason`` field generator
    reports as ``PERMISSIONS`` (and what makes the lifecycle read WAITING rather than
    RUNNING). This is the only check that exercises that event wiring against the live
    opencode binary -- the sdk type stubs disagree with the binary on the event name, so
    a unit test alone would not catch a regression to the real contract. The idle-side
    ``END_OF_TURN`` (marker absent) is asserted right after create.
    """
    profile = _OpenCodePermissionReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"opencode-perm-{get_short_random_string()}"
    try:
        state_dir = _create_agent(profile, ctx, agent_name)
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
        # The session stays busy (active present) while a prompt is open; the block
        # marker is what makes the lifecycle read WAITING rather than RUNNING.
        assert active_marker.exists(), "expected the active marker present while blocked on an approval prompt"
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        if ctx.teardown is not None:
            ctx.teardown()


def _create_agent(
    profile: _OpenCodeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, *extra_args: str
) -> Path:
    """Create the agent and return its state dir."""
    create = profile.run_mngr(
        ctx,
        "create",
        agent_name,
        profile.agent_type,
        "--no-connect",
        "--yes",
        *profile.create_extra_args(ctx),
        *extra_args,
        timeout=_CREATE_TIMEOUT_SECONDS,
    )
    assert create.returncode == 0, f"create failed:\n{create.stdout}\n{create.stderr}"
    return _opencode_agent_state_dir(ctx.host_dir)


def _wait_for_listed_turn_end(
    profile: _OpenCodeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, state_dir: Path, turn: str
) -> tuple[str, str] | None:
    """Wait for ``turn`` to write turn_end, then return the kind and native type ``mngr list`` publishes."""
    assert poll_until(
        condition=lambda: read_turn_end_in(state_dir) is not None,
        timeout=_TURN_TIMEOUT_SECONDS,
        poll_interval=0.5,
    ), f"{turn} never wrote turn_end"
    listing = profile.run_mngr(
        ctx, "list", "--provider", "local", "--format", "json", timeout=_LIFECYCLE_TIMEOUT_SECONDS
    )
    assert listing.returncode == 0, f"mngr list failed:\n{listing.stdout}\n{listing.stderr}"
    (listed_agent,) = [agent for agent in json.loads(listing.stdout)["agents"] if agent["name"] == agent_name]
    turn_end = listed_agent.get("turn_end")
    return None if turn_end is None else (turn_end["kind"], turn_end["native_type"])


def _raw_transcript_lines(state_dir: Path) -> list[str]:
    raw_transcript = state_dir / RAW_TRANSCRIPT_RELATIVE_PATH
    return raw_transcript.read_text().splitlines() if raw_transcript.exists() else []


def _a_bash_tool_call_is_running(state_dir: Path, lines_before: int) -> bool:
    new_lines = _raw_transcript_lines(state_dir)[lines_before:]
    return any('"tool":"bash"' in line and '"status":"running"' in line for line in new_lines)


def _server_url_and_root_session(state_dir: Path) -> tuple[str, str]:
    """The base URL of the agent's opencode server and its root session id."""
    port = (state_dir / SERVER_PORT_FILENAME).read_text().strip()
    return f"http://127.0.0.1:{port}", (state_dir / ROOT_SESSION_FILENAME).read_text().strip()


def _abort_root_session(state_dir: Path) -> None:
    """Stop the running turn through the agent's server's `session.abort` endpoint."""
    server_url, session_id = _server_url_and_root_session(state_dir)
    request = urllib.request.Request(f"{server_url}/session/{session_id}/abort", method="POST")
    with urllib.request.urlopen(request, timeout=_LIFECYCLE_TIMEOUT_SECONDS) as response:
        assert response.status == 200, f"session.abort answered {response.status}"


def _reject_pending_permission(state_dir: Path) -> None:
    """Decline the root session's pending approval prompt through the agent's server's `permission.reply` endpoint."""
    server_url, session_id = _server_url_and_root_session(state_dir)
    with urllib.request.urlopen(f"{server_url}/permission", timeout=_LIFECYCLE_TIMEOUT_SECONDS) as response:
        pending = json.loads(response.read())
    (request_id,) = [request["id"] for request in pending if request["sessionID"] == session_id]
    request = urllib.request.Request(
        f"{server_url}/permission/{request_id}/reply",
        data=json.dumps({"reply": "reject"}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=_LIFECYCLE_TIMEOUT_SECONDS) as response:
        assert response.status == 200, f"permission.reply answered {response.status}"


@pytest.mark.release
@pytest.mark.harness_internals(harness="opencode")
@pytest.mark.tmux
@pytest.mark.rsync
# Known-flaky: the same create-readiness timeout as test_opencode_agent_full_lifecycle.
@pytest.mark.flaky
@pytest.mark.timeout(900)
def test_opencode_turn_end_reports_a_completed_and_an_interrupted_turn(tmp_path: Path) -> None:
    """A finished reply publishes turn_end COMPLETED with its finish, and an aborted turn INTERRUPTED.

    Pins that opencode goes idle after a reply whose ``finish`` is ``stop``, and that a ``session.abort`` during a
    tool call ends the turn with ``MessageAbortedError``.
    """
    profile = _OpenCodeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"opencode-turn-end-{get_short_random_string()}"
    try:
        state_dir = _create_agent(profile, ctx, agent_name)

        send_expecting_success(profile, ctx, agent_name, _SHORT_REPLY_PROMPT)
        completed_turn_end = _wait_for_listed_turn_end(profile, ctx, agent_name, state_dir, "the first turn")

        lines_before = len(_raw_transcript_lines(state_dir))
        send_expecting_success(profile, ctx, agent_name, _LONG_BASH_PROMPT)
        assert poll_until(
            condition=lambda: _a_bash_tool_call_is_running(state_dir, lines_before),
            timeout=_TURN_TIMEOUT_SECONDS,
            poll_interval=0.5,
        ), "the second turn never started its bash tool call"
        _abort_root_session(state_dir)
        interrupted_turn_end = _wait_for_listed_turn_end(profile, ctx, agent_name, state_dir, "the aborted turn")
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        if ctx.teardown is not None:
            ctx.teardown()

    assert completed_turn_end == ("COMPLETED", "stop"), (
        f"mngr list published turn_end {completed_turn_end!r} after a finished reply; consumer: {_TURN_END_CONSUMER}"
    )
    assert interrupted_turn_end == ("INTERRUPTED", "MessageAbortedError"), (
        f"mngr list published turn_end {interrupted_turn_end!r} after session.abort; consumer: {_TURN_END_CONSUMER}"
    )


@pytest.mark.release
@pytest.mark.harness_internals(harness="opencode")
@pytest.mark.tmux
@pytest.mark.rsync
# Known-flaky: the same create-readiness timeout as test_opencode_agent_full_lifecycle.
@pytest.mark.flaky
@pytest.mark.timeout(900)
def test_opencode_turn_end_reports_a_declined_permission_as_interrupted(tmp_path: Path) -> None:
    """Declining an approval prompt publishes turn_end INTERRUPTED with ``PermissionRejectedError``.

    Pins that opencode reports the decline as a root ``permission.replied`` with reply ``reject`` and then ends the
    turn without a final reply or an error.
    """
    profile = _OpenCodePermissionReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"opencode-turn-end-declined-{get_short_random_string()}"
    try:
        state_dir = _create_agent(profile, ctx, agent_name)
        send_expecting_success(profile, ctx, agent_name, _BASH_TRIGGER_PROMPT)
        assert poll_until(
            condition=(state_dir / PERMISSIONS_WAITING_FILENAME).exists,
            timeout=_PERMISSION_MARKER_TIMEOUT_SECONDS,
            poll_interval=0.5,
        ), "the bash tool call never blocked on an approval prompt"
        _reject_pending_permission(state_dir)
        declined_turn_end = _wait_for_listed_turn_end(profile, ctx, agent_name, state_dir, "the declined turn")
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        if ctx.teardown is not None:
            ctx.teardown()

    assert declined_turn_end == ("INTERRUPTED", "PermissionRejectedError"), (
        f"mngr list published turn_end {declined_turn_end!r} after a declined permission; consumer: {_TURN_END_CONSUMER}"
    )


def _send_and_wait_for_turn_end(
    profile: _OpenCodeReleaseProfile, ctx: AgentReleaseContext, agent_name: str, state_dir: Path, prompt: str
) -> list[str]:
    """Send ``prompt``, wait until the lifecycle plugin records the end of its turn, and return the raw transcript lines the turn added."""
    lines_before = len(_raw_transcript_lines(state_dir))
    # The previous turn's turn_end goes so the poll below sees only this turn's.
    (state_dir / TURN_END_FILE_NAME).unlink(missing_ok=True)
    send_expecting_success(profile, ctx, agent_name, prompt)
    assert poll_until(
        condition=lambda: read_turn_end_in(state_dir) is not None, timeout=_TURN_TIMEOUT_SECONDS, poll_interval=0.5
    ), f"the turn {prompt!r} never wrote turn_end"
    return _raw_transcript_lines(state_dir)[lines_before:]


def _read_common_transcript_user_messages(state_dir: Path) -> list[str]:
    common_transcript = state_dir / COMMON_TRANSCRIPT_RELATIVE_PATH
    if not common_transcript.exists():
        return []
    records = [json.loads(line) for line in common_transcript.read_text().splitlines() if line.strip()]
    return [
        record.get("message", "")
        for record in records
        if record.get("type") == "step" and record.get("source") == "user"
    ]


def _is_user_prompt_in_common_transcript(state_dir: Path, prompt: str) -> bool:
    return any(prompt in message for message in _read_common_transcript_user_messages(state_dir))


@pytest.mark.release
@pytest.mark.harness_internals(harness="opencode")
@pytest.mark.tmux
@pytest.mark.rsync
@pytest.mark.timeout(1200)
def test_opencode_upgrade_restarts_an_idle_agent_on_its_packaged_plugin_and_keeps_its_conversation(
    tmp_path: Path,
) -> None:
    """An idle agent with a stale lifecycle plugin comes back from ``mngr upgrade`` on the packaged one, in its session.

    The restarted agent recalls a secret it was told before the upgrade, and the upgraded plugin records that turn
    and rebuilds the common transcript with the turns the previous plugin recorded.
    """
    profile = _OpenCodeReleaseProfile()
    reason = profile.unavailable_reason()
    if reason is not None:
        pytest.skip(reason)

    ctx = profile.setup(tmp_path)
    agent_name = f"opencode-upgrade-{get_short_random_string()}"
    secret = f"SECRET-{get_short_random_string()}"
    remember_prompt = f"Remember this exact secret for later: {secret}. Reply with just OK."
    try:
        state_dir = _create_agent(
            profile, ctx, agent_name, "-S", f"agent_ready_timeout={_FIRST_RUN_READY_TIMEOUT_SECONDS}"
        )
        _send_and_wait_for_turn_end(profile, ctx, agent_name, state_dir, remember_prompt)

        plugin_path = get_opencode_plugin_path(get_opencode_config_dir(state_dir))
        packaged_plugin = plugin_path.read_text()
        plugin_path.write_text(f"{packaged_plugin}\n// stale-{get_short_random_string()}\n")
        data_before = (state_dir / "data.json").read_bytes()
        root_session_before = (state_dir / ROOT_SESSION_FILENAME).read_text()

        upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert upgrade.returncode == 0, f"upgrade failed:\n{upgrade.stdout}\n{upgrade.stderr}"
        assert f"upgraded {agent_name} (restarted)" in upgrade.stdout, upgrade.stdout
        assert plugin_path.read_text() == packaged_plugin
        assert (state_dir / "data.json").read_bytes() == data_before
        assert (state_dir / ROOT_SESSION_FILENAME).read_text() == root_session_before

        recall_turn_lines = _send_and_wait_for_turn_end(profile, ctx, agent_name, state_dir, _RECALL_PROMPT)
        assert any(secret in line for line in recall_turn_lines), (
            "the restarted agent did not recall the secret; its turn recorded:\n" + "\n".join(recall_turn_lines)
        )
        assert poll_until(
            condition=lambda: _is_user_prompt_in_common_transcript(state_dir, _RECALL_PROMPT),
            timeout=_TURN_TIMEOUT_SECONDS,
            poll_interval=0.5,
        ), "the upgraded plugin never rebuilt the common transcript"
        assert _is_user_prompt_in_common_transcript(state_dir, remember_prompt), (
            "the common transcript the upgraded plugin rebuilt lost the turn from before the upgrade: "
            f"{_read_common_transcript_user_messages(state_dir)}"
        )

        second_upgrade = profile.run_mngr(ctx, "upgrade", agent_name, timeout=_UPGRADE_TIMEOUT_SECONDS)

        assert second_upgrade.returncode == 0, f"upgrade failed:\n{second_upgrade.stdout}\n{second_upgrade.stderr}"
        assert f"current {agent_name}" in second_upgrade.stdout, second_upgrade.stdout
    finally:
        profile.run_mngr(ctx, "destroy", agent_name, "--force", timeout=_LIFECYCLE_TIMEOUT_SECONDS)
        if ctx.teardown is not None:
            ctx.teardown()
